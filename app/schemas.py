from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator

PositiveID = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]


class GenerateRequest(BaseModel):
    user_id: PositiveID
    model_slug: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=10000)
    ref_images: list[AnyHttpUrl] = Field(default_factory=list, max_length=100)


class PaymentEvent(BaseModel):
    payment_id: str = Field(min_length=1, max_length=128)
    user_id: PositiveID
    tokens: Annotated[int, Field(strict=True, gt=0, le=1000000000)]
    status: Literal["completed", "pending", "failed"]


class ProviderEvent(BaseModel):
    attempt_id: UUID
    external_id: str = Field(min_length=1, max_length=128)
    status: Literal["completed", "failed"]
    error_type: Literal["technical", "moderation", "payload"] | None = None
    result_url: AnyHttpUrl | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ProviderEvent":
        if self.status == "completed" and (self.result_url is None or self.error_type is not None):
            raise ValueError("completed requires result_url and no error_type")
        if self.status == "failed" and (self.error_type is None or self.result_url is not None):
            raise ValueError("failed requires error_type and no result_url")
        return self
