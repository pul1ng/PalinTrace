"""Branch-owned input for decisions at a restored Resume boundary."""

from copy import deepcopy


def build_resume_messages(session):
    """Copy the saved prefix without sharing nested messages with its source."""
    return deepcopy(session.prefix.messages)
