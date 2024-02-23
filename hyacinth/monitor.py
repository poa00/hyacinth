import logging
from datetime import datetime, timedelta
from typing import Sequence

from apscheduler.job import Job
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

from hyacinth.db.crud.listing import get_last_listing as get_last_listing_from_db
from hyacinth.db.crud.listing import get_listings as get_listings_from_db
from hyacinth.db.models import Listing, SearchSpec
from hyacinth.db.session import Session
from hyacinth.metrics import METRIC_POLL_JOB_EXECUTION_COUNT, write_metric
from hyacinth.models import BaseListing
from hyacinth.scheduler import get_async_scheduler
from hyacinth.settings import get_settings
from hyacinth.util.crash_report import save_poll_failure_report

settings = get_settings()
_logger = logging.getLogger(__name__)


class SearchMonitor:
    def __init__(self) -> None:
        self.scheduler = get_async_scheduler()
        self.search_specs: list[SearchSpec] = []
        self.search_spec_job_mapping: dict[int, Job] = {}  # SearchSpec id -> job
        self.search_spec_ref_count: dict[int, int] = {}  # SearchSpec id -> ref count

    def register_search(self, search_spec: SearchSpec) -> None:
        # check if there is already a scheduled task to poll this search
        if search_spec.id in self.search_spec_job_mapping:
            _logger.info("Search already exists, not registering new search")
            self.search_spec_ref_count[search_spec.id] += 1
            return

        # otherwise schedule a job to periodically check results and write them to the db
        _logger.info(f"Scheduling job for new search! {search_spec}")
        self.search_spec_job_mapping[search_spec.id] = self.scheduler.add_job(
            self.poll_search,
            kwargs={"search_spec": search_spec},
            trigger=IntervalTrigger(
                seconds=search_spec.plugin.polling_interval(search_spec.search_params)
            ),
            next_run_time=datetime.now(),
        )
        self.search_spec_ref_count[search_spec.id] = 1

    def remove_search(self, search_spec: SearchSpec) -> None:
        self.search_spec_ref_count[search_spec.id] -= 1
        if self.search_spec_ref_count[search_spec.id] == 0:
            # there are no more notifiers looking at this search, remove the monitoring job
            _logger.debug(f"Removing search from monitor {search_spec}")
            job = self.search_spec_job_mapping[search_spec.id]
            self.scheduler.remove_job(job.id)
            del self.search_spec_job_mapping[search_spec.id]
            del self.search_spec_ref_count[search_spec.id]

    async def get_listings(
        self, search_spec: SearchSpec, after_time: datetime
    ) -> Sequence[Listing]:
        with Session() as session:
            return get_listings_from_db(session, search_spec.id, after_time)

    async def poll_search(self, search_spec: SearchSpec) -> None:
        if settings.disable_search_polling:
            _logger.debug(f"Search polling is disabled, would poll search {search_spec}")
            return

        with Session() as session:
            after_time = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")) - timedelta(
                hours=settings.notifier_backdate_time_hours
            )
            last_listing = get_last_listing_from_db(session, search_spec.id)
            if last_listing is not None:
                # resume at the last listing time if it was more recent than the backdate time
                after_time = max(last_listing.created_at, after_time)
                _logger.debug(
                    f"Found recent listing at {last_listing.created_at}, resuming at {after_time}."
                )

            listings = await self.__safe_poll_search(search_spec, after_time)
            session.add_all(
                Listing.from_base_listing(listing, search_spec.id) for listing in listings
            )
            session.commit()
            _logger.debug(f"Found {len(listings)} since {after_time} for search_spec={search_spec}")

    async def __safe_poll_search(
        self, search_spec: SearchSpec, after_time: datetime
    ) -> list[BaseListing]:
        _logger.debug(f"Polling search {search_spec} since {after_time}")
        listings: list[BaseListing] | None = None
        try:
            listings = await search_spec.plugin.get_listings(search_spec.search_params, after_time)
        except Exception as e:
            _logger.exception(f"Error polling search {search_spec}")
            save_poll_failure_report(e)

        await self.__write_poll_execution_metric(search_spec.plugin_path, listings is not None)
        return listings or []

    async def __write_poll_execution_metric(self, plugin_path: str, success: bool) -> None:
        labels = {"success": str(success).lower(), "plugin": plugin_path}
        write_metric(METRIC_POLL_JOB_EXECUTION_COUNT, 1, labels)

    def __del__(self) -> None:
        for search_spec in self.search_spec_job_mapping:
            job = self.search_spec_job_mapping[search_spec]
            self.scheduler.remove_job(job.id)
