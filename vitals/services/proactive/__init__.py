"""The proactive layer — the part of Vitals that speaks first.

Four deliberately separate seams, so changing one leaves the others untouched:

  * ``channels``  — *how* a message leaves the app. One ``Notifier``
    protocol; Telegram is merely the first implementation.
  * ``delivery``  — *whether* it may leave: daily budget, quiet hours, dedupe,
    and the journal row that makes all three enforceable.
  * ``inbound``   — what comes back: taps, replies, and free text into ``signals``.
  * ``compose``   — *what* is said: a message is a list of blocks, and the
    model owns exactly one of them. ``brief`` is the first message built that way.
  * ``day_plan``  — what kind of day it is: the week template, the 23:45 evening
    block, and the one-tap answers both it and the brief collect.
  * ``nudges``    — when a condition is worth a message at all.

Nothing above ``channels`` knows the word "telegram"; nothing below ``delivery``
knows about budgets.
"""
