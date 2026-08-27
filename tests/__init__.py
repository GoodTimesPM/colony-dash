"""Tests for the colony.

Standard-library `unittest`, on purpose. The dependency surface of this project
is four packages and none of them is a test runner; a test suite that needs an
install before it runs is a test suite a stranger cloning the repo does not run.

    py -m unittest discover -s tests -v

Nothing here touches the real ledger under `.colony/`. Every test that needs a
database builds one in a temp directory and deletes it, and the two tests that
would otherwise spawn the `claude` CLI intercept the call instead.
"""
