import type { SectionConfigOverrides } from "./types";

const imageSource: SectionConfigOverrides = {
  base: {
    sectionDocs: "/configuration/advanced/reference",
    restartRequired: [],
    fieldOrder: ["stream", "match_threshold", "search_before", "search_after"],
    hiddenFields: [],
    advancedFields: ["match_threshold", "search_before", "search_after"],
    uiSchema: {
      stream: {
        "ui:size": "xs",
        "ui:options": { enumI18nPrefix: "imageSourceStream" },
      },
    },
  },
  global: {
    restartRequired: [
      "stream",
      "match_threshold",
      "search_before",
      "search_after",
    ],
  },
  camera: {
    restartRequired: [],
  },
};

export default imageSource;
