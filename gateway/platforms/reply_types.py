"""Handler-result ``str`` subclasses the adapter send path special-cases.

Subclassing ``str`` keeps them transparent to everything that treats a handler result as text;
``isinstance`` lets the send path act on them. ``gateway.platforms.base`` re-exports every name
here, which stays the import path callers use.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # string annotations only; base imports this module (cycle)
    from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)


class EphemeralReply(str):
    """System-notice reply that auto-deletes after ``ttl_seconds`` on platforms implementing
    ``delete_message`` (others leave it). ``None`` ttl uses ``display.ephemeral_system_ttl``
    (``0`` disables globally). Subclassing ``str`` keeps it transparent to everything that
    treats handler results as text; ``isinstance`` lets the send path schedule deletion."""
    ttl_seconds: Optional[int]

    def __new__(cls, text: str, ttl_seconds: Optional[int] = None):
        instance = super().__new__(cls, text)
        instance.ttl_seconds = ttl_seconds
        return instance

    @property
    def text(self) -> str:
        """The underlying text (explicit form of ``str(reply)``)."""
        return str.__str__(self)


class SenderOnlyNotice(str):
    """Gateway notice meant only for the person whose message caused it (the unexpected-silence
    notice), carrying that sender's ``user_id``. The runner builds one only for a shared
    conversation; :func:`send_sender_only_notice` delivers it privately where the adapter can,
    and the caller's normal public send is the fallback, so the sender always gets it."""
    user_id: str

    def __new__(cls, text: str, user_id: str):
        instance = super().__new__(cls, text)
        instance.user_id = user_id
        return instance

    @property
    def text(self) -> str:
        """The underlying text (explicit form of ``str(notice)``)."""
        return str.__str__(self)


def keep_sender_only(original: Any, text: Any) -> Any:
    """``text`` re-wrapped as a :class:`SenderOnlyNotice` for ``original``'s sender when
    ``original`` was one; ``text`` unchanged otherwise. String transforms on the delivery path
    (sanitizing, media extraction, a footer) return plain ``str`` and would drop the routing."""
    if isinstance(original, SenderOnlyNotice) and isinstance(text, str) and text \
            and not isinstance(text, SenderOnlyNotice):
        return SenderOnlyNotice(text, original.user_id)
    return text


async def send_sender_only_notice(
    adapter: BasePlatformAdapter, chat_id: str, content: Any,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[SendResult]:
    """Deliver a :class:`SenderOnlyNotice` privately to its sender through the adapter's
    ``send_private_notice``. Returns the successful result, or ``None`` when ``content`` is not
    one, the adapter keeps the base (public) ``send_private_notice``, or the private send failed
    or raised: the caller then makes its normal public send, because a lost notice would bring
    back the silent drop of #110952."""
    from gateway.platforms.base import BasePlatformAdapter

    if not isinstance(content, SenderOnlyNotice) or not content.user_id:
        return None
    if type(adapter).send_private_notice is BasePlatformAdapter.send_private_notice:
        return None
    platform = getattr(adapter.platform, "value", adapter.platform)
    try:
        result = await adapter.send_private_notice(
            chat_id, content.user_id, content.text, metadata=metadata)
    except Exception:  # the public send is the fallback; the traceback says why
        logger.warning("[%s] Private sender-only notice raised in chat %s; sending it to the chat"
                       " instead", platform, chat_id, exc_info=True)
        return None
    if getattr(result, "success", False):
        logger.info("Sender-only notice delivered privately: platform=%s chat=%s", platform, chat_id)
        return result
    logger.warning("[%s] Private sender-only notice failed in chat %s (%s); sending it to the chat"
                   " instead", platform, chat_id, getattr(result, "error", None) or "no result")
    return None
