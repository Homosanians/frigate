type PtzFeature =
  | "pt"
  | "zoom"
  | "pt-r"
  | "zoom-r"
  | "zoom-a"
  | "pt-r-fov"
  | "focus"
  | "home"
  | "home-set";

export type OnvifProfile = {
  name: string;
  token: string;
};

export type PtzPreset = {
  token: string;
  name: string;
};

export type CameraPtzInfo = {
  name: string;
  features: PtzFeature[];
  presets: string[];
  preset_details?: PtzPreset[];
  max_presets?: number | null;
  profiles: OnvifProfile[];
};

/** What a camera's ONVIF device service reports; null means it did not answer. */
export type OnvifDeviceInfo = {
  manufacturer: string | null;
  model: string | null;
  firmware_version: string | null;
  /** ONVIF conformance profiles (S, G, T, M, ...), not media profiles. */
  conformance_profiles: string[] | null;
  date_time: {
    type: string | null;
    timezone: string | null;
    daylight_savings: boolean | null;
    utc_time: string | null;
    /** Camera clock minus Frigate's clock. */
    offset_seconds: number | null;
  } | null;
  ntp: {
    from_dhcp: boolean | null;
    servers: string[];
  } | null;
  /** The camera's time sync config and the outcome of the last attempt. */
  time_sync: {
    enabled: boolean;
    ntp_server: string | null;
    timezone: string | null;
    posix_timezone: string | null;
    last_result: {
      time: number;
      success: boolean;
      message: string | null;
    } | null;
  };
};
