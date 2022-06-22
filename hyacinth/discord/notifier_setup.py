import logging
import re
import traceback
from typing import TYPE_CHECKING, Any

from discord import Message

from hyacinth.db.notifier import save_notifier as save_notifier_to_db
from hyacinth.discord.thread_interaction import FMT_USER, Question, ThreadInteraction
from hyacinth.models import SearchSpec, SearchSpecSource
from hyacinth.notifier import DiscordNotifier
from hyacinth.sources.craigslist import CraigslistSearchParams
from hyacinth.util.craigslist import get_areas

if TYPE_CHECKING:
    # avoid circular import
    from hyacinth.discord.discord_bot import DiscordNotifierBot

_logger = logging.getLogger(__name__)


class CraigslistNotifierSetupInteraction(ThreadInteraction):
    def __init__(self, bot: "DiscordNotifierBot", initiating_message: Message) -> None:
        super().__init__(
            bot,
            initiating_message,
            thread_title="Create a new Craigslist notifier",
            first_message=f"Hi {FMT_USER}! Let's get that set up for you.",
            questions=[
                Question(
                    key="area",
                    prompt=(
                        "Which area of Craigslist would you like to search? Available"
                        f" areas:\n```{self.available_areas}```"
                    ),
                    validator=CraigslistNotifierSetupInteraction.validate_areas,
                ),
                Question(
                    key="category",
                    prompt=(
                        "Which category of Craigslist would you like to search? This is the string"
                        " in the Craigslist URL after `/search`. For example, `mca` for motorcycles"
                        ' or `sss` for general "for sale".'
                    ),
                    validator=CraigslistNotifierSetupInteraction.validate_category,
                ),
                Question(
                    key="max_distance_miles",
                    prompt=(
                        "What is the maximum distance away (in miles) that you would like to show"
                        " results for?"
                    ),
                    validator=int,
                ),
            ],
        )

    async def finish(self) -> dict[str, Any]:
        try:
            created_notifier = self.configure_notifier()
        except Exception:
            await self.send(
                f"Sorry {FMT_USER}! Something went wrong while configuring the notifier for this"
                f" channel. ```{traceback.format_exc()}```"
            )
            await super().finish()
            raise

        # if this is the first search set up on this channel, the notifier starts paused. add some
        # helpful information about that for the user if necessary.
        created_notifier_part = (
            "\n\nNotifications are currently paused. When you are done configuring your desired"
            " filter rules, start sending notifications with `$start`."
            if created_notifier
            else ""
        )
        await self.send(
            f"{self.bot.thank()} {FMT_USER}! I've set up a search for new Craigslist listings on"
            f" this channel.{created_notifier_part}"
        )

        return await super().finish()

    def configure_notifier(self) -> bool:
        search_params = dict(self._answers)
        area = get_areas()[search_params.pop("area")]
        search_params["site"] = area.site
        search_params["nearby_areas"] = area.nearby_areas

        search_spec = SearchSpec(
            source=SearchSpecSource.CRAIGSLIST,
            search_params=CraigslistSearchParams.parse_obj(search_params),
        )
        _logger.debug(f"Parsed search spec from answers {search_spec}")

        channel = self.initiating_message.channel
        created_notifier = False
        if channel.id not in self.bot.notifiers:
            _logger.info(f"Creating notifier for channel {channel.id}")
            self.bot.notifiers[channel.id] = DiscordNotifier(
                channel, self.bot.monitor, DiscordNotifier.Config(paused=True)
            )
            created_notifier = True
        self.bot.notifiers[channel.id].create_search(search_spec)
        save_notifier_to_db(self.bot.notifiers[channel.id])
        return created_notifier

    @property
    def available_areas(self) -> str:
        return "\n".join([f"{i + 1}. {area}" for i, area in enumerate(get_areas().keys())])

    @staticmethod
    def validate_areas(v: str) -> str:
        areas = get_areas()
        try:
            selection = int(v) - 1
            selected_area = list(get_areas())[selection]
        except ValueError:
            if v in areas:
                selected_area = v
            else:
                raise

        return selected_area

    @staticmethod
    def validate_category(v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9]+$", v):
            raise ValueError("Category must be alphanumeric")
        return v