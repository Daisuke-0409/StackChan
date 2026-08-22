"""Marks this directory as a package so unittest discover can reach it.

Without this file `python -m unittest discover` finds zero tests here --
it imports what it walks, and an unimportable directory is skipped in
silence. The suite still passed when a module was named explicitly, which
is why nobody noticed: the documented command reported "Ran 0 tests" and
that reads almost exactly like success.
"""
