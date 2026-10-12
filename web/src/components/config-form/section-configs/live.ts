import type { SectionConfigOverrides } from "./types";

const live: SectionConfigOverrides = {
  base: {
    sectionDocs: "/configuration/live",
    restartRequired: [],
    fieldOrder: ["streams", "transcode", "cache", "height", "quality"],
    fieldGroups: {},
    hiddenFields: ["enabled_in_config"],
    advancedFields: ["height", "quality"],
  },
  global: {
    restartRequired: ["streams", "cache", "height", "quality"],
    hiddenFields: ["streams", "transcode"],
  },
  camera: {
    restartRequired: ["cache", "height", "quality"],
    orderedMaps: ["streams"],
    uiSchema: {
      streams: {
        "ui:field": "LiveStreamsField",
        "ui:options": {
          label: false,
          suppressDescription: true,
        },
      },
      transcode: {
        "ui:field": "LiveTranscodeField",
        "ui:options": {
          label: false,
          suppressDescription: true,
        },
      },
    },
  },
};

export default live;
