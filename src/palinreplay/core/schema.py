"""Shared immutable configuration model."""

from pydantic import BaseModel, ConfigDict


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
