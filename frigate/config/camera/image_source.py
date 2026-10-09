from enum import Enum

from pydantic import Field

from ..base import FrigateBaseModel

__all__ = ["ImageSourceConfig", "ImageSourceStreamEnum"]


class ImageSourceStreamEnum(str, Enum):
    detect = "detect"
    main = "main"
    auto = "auto"


class ImageSourceConfig(FrigateBaseModel):
    stream: ImageSourceStreamEnum = Field(
        default=ImageSourceStreamEnum.detect,
        title="Image source stream",
        description="Stream that saved images (faces, license plates, classification crops, snapshots) are taken from. 'detect' uses the detect stream, 'main' replaces them with a frame from the main stream when one can be fetched, 'auto' only does so when that frame is larger than the detect stream.",
    )
    match_threshold: float = Field(
        default=0.9,
        title="Match threshold",
        description="The main and detect streams are not in sync, so the main stream is searched for the frame that shows the same moment as the detect image. This is the minimum similarity (normalized correlation, 0 to 1) a main stream frame needs to be used. Higher values keep the detect image more often, lower values risk saving a frame from a different moment.",
        ge=0.0,
        le=1.0,
    )
    search_before: float = Field(
        default=4.0,
        title="Search before",
        description="Seconds before the detect frame time that the main stream recording is searched for the matching frame. Raise this if the recording runs ahead of the detect stream.",
        ge=0.0,
        le=15.0,
    )
    search_after: float = Field(
        default=4.0,
        title="Search after",
        description="Seconds after the detect frame time that the main stream recording is searched for the matching frame. Saved images are replaced only once this much of the recording has been written, so higher values delay the replacement.",
        ge=0.0,
        le=15.0,
    )
