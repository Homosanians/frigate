/**
 * ONVIF tab of the camera Debug view -- MEDIUM tier.
 *
 * Cameras report values with no spaces to wrap at, such as a POSIX timezone.
 * They must wrap inside the narrow debug panel instead of running past it.
 */

import { test, expect } from "../../fixtures/frigate-test";

const DEBUG_URL = "/?debug=true#front_door";

const INFO = {
  manufacturer: "Dahua",
  model: "DH-SD1A203T",
  firmware_version: "2.840.0000000.12.R",
  conformance_profiles: ["S", "G", "T"],
  date_time: {
    type: "Manual",
    timezone: "GMT+03:00PDT,M1.1.1/00:00:00,M1.1.2/00:00:00",
    daylight_savings: false,
    utc_time: "2026-10-09T19:42:41Z",
    offset_seconds: 0,
  },
  ntp: { from_dhcp: false, servers: ["pool.ntp.org"] },
  time_sync: {
    enabled: false,
    ntp_server: null,
    timezone: null,
    posix_timezone: null,
    last_result: null,
  },
};

test.describe("ONVIF debug info @medium @mobile", () => {
  test("a long timezone wraps inside its row", async ({ frigateApp }) => {
    await frigateApp.installDefaults({
      config: { cameras: { front_door: { onvif: { host: "10.0.0.5" } } } },
    });
    await frigateApp.page.route("**/api/front_door/onvif/info**", (route) =>
      route.fulfill({ json: INFO }),
    );

    // the debug panel is a quarter of the window, as narrow as it gets
    if (!frigateApp.isMobile) {
      await frigateApp.page.setViewportSize({ width: 1280, height: 800 });
    }

    await frigateApp.goto(DEBUG_URL);
    await frigateApp.page.getByRole("tab", { name: "ONVIF" }).click();

    const value = frigateApp.page.getByText(INFO.date_time.timezone);
    await expect(value).toBeVisible();

    const row = value.locator("xpath=ancestor::div[1]");
    const rowBox = await row.boundingBox();
    const valueBox = await value.boundingBox();

    expect(valueBox!.x + valueBox!.width).toBeLessThanOrEqual(
      rowBox!.x + rowBox!.width + 1,
    );
  });
});
