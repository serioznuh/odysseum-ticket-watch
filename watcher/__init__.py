"""Ticket-sale advance-notification watcher for Pathé cinemas (France).

Watches the public www.pathe.fr JSON API for a film's ticket-sale opening
(`salesOpeningDatetime`), bookable sessions at one target cinema, and external
news leads, then notifies via Telegram.
"""

__version__ = "1.0.0"
