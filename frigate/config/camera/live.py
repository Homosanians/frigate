from pydantic import Field, field_validator

from frigate.util.live_streams import DEFAULT_TRANSCODE_QUALITIES

from ..base import FrigateBaseModel

__all__ = [
    "CameraLiveConfig",
    "LiveCacheConfig",
    "LiveTranscodeConfig",
    "LiveTranscodeQualityConfig",
]


class LiveTranscodeQualityConfig(FrigateBaseModel):
    height: int = Field(
        ge=144,
        le=2160,
        title="Height",
        description="Output height in pixels; width follows the source aspect ratio.",
    )
    bitrate: int = Field(
        ge=64,
        title="Bitrate",
        description="Target and maximum video bitrate in kbps.",
    )


class LiveTranscodeConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable transcoded streams",
        description="Add lower-quality live streams that go2rtc transcodes in real time while someone is watching.",
    )
    source: str | None = Field(
        default=None,
        title="Source stream",
        description="go2rtc stream to transcode. Defaults to the first live stream.",
    )
    qualities: list[LiveTranscodeQualityConfig] = Field(
        default_factory=lambda: [
            LiveTranscodeQualityConfig(**quality)
            for quality in DEFAULT_TRANSCODE_QUALITIES
        ],
        title="Qualities",
        description="One transcoded stream is added per quality.",
    )

    @field_validator("qualities")
    @classmethod
    def validate_unique_heights(
        cls, qualities: list[LiveTranscodeQualityConfig]
    ) -> list[LiveTranscodeQualityConfig]:
        heights = [quality.height for quality in qualities]

        if len(heights) != len(set(heights)):
            raise ValueError("Transcoded stream heights must be unique.")

        return qualities


class LiveCacheConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable live stream cache",
        description="Keep the frames since the last keyframe of each live stream in memory, so MSE live view starts at once instead of waiting for the next keyframe.",
    )


class CameraLiveConfig(FrigateBaseModel):
    streams: dict[str, str] = Field(
        default_factory=list,
        title="Live stream names",
        description="Mapping of configured stream names to restream/go2rtc names used for live playback.",
    )
    transcode: LiveTranscodeConfig = Field(
        default_factory=LiveTranscodeConfig,
        title="Transcoded streams",
        description="Lower-quality live streams transcoded on demand by go2rtc.",
    )
    cache: LiveCacheConfig = Field(
        default_factory=LiveCacheConfig,
        title="Live stream cache",
        description="Instant start for MSE live view from a cache of recent frames.",
    )
    height: int = Field(
        default=720,
        title="Live height",
        description="Height (pixels) to render the jsmpeg live stream in the Web UI; must be <= detect stream height.",
    )
    quality: int = Field(
        default=8,
        ge=1,
        le=31,
        title="Live quality",
        description="Encoding quality for the jsmpeg stream (1 highest, 31 lowest).",
    )
