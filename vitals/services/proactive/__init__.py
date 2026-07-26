"""The proactive layer — the part of Vitals that speaks first.

Four deliberately separate seams, so changing one leaves the others untouched:

  * ``channels``  — *how* a message leaves the app (шов 1). One ``Notifier``
    protocol; Telegram is merely the first implementation.
  * ``delivery``  — *whether* it may leave: daily budget, quiet hours, dedupe,
    and the journal row that makes all three enforceable.
  * ``inbound``   — what comes back: taps, replies, and free text into ``signals``.
  * (прогон 3+)  ``compose`` / ``brief`` / ``nudges`` — *what* is said.

Nothing above ``channels`` knows the word "telegram"; nothing below ``delivery``
knows about budgets.
"""
