"""Versioned data-portability archive primitives.

The package deliberately has no web or persistence dependencies. Delivery
boundaries decide authorization and storage; the child modules define and
protect the on-wire archive. Import those modules explicitly so the package
root never creates a parent/child import cycle.
"""
