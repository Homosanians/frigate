/**
 * Click to move in the live view -- MEDIUM tier.
 *
 * The button appears when the camera supports the relative moves of its
 * onvif.relative_move.mode: field of view moves by default, generic moves in
 * generic mode.
 */

import { test, expect } from "../fixtures/frigate-test";
import type { FrigateApp } from "../fixtures/frigate-test";

const CLICK_TO_MOVE = "Click in the frame to center the camera";
const MOVE_LEFT = "Move PTZ camera to the left";

// "default" leaves onvif.relative_move out of the config
async function openLiveView(
  frigateApp: FrigateApp,
  mode: "default" | "fov" | "generic",
  features: string[],
) {
  const relativeMove =
    mode === "generic"
      ? { mode, pan_scale: -0.24, tilt_scale: -0.27 }
      : { mode };

  await frigateApp.installDefaults({
    config: {
      cameras: {
        front_door: {
          onvif: {
            host: "10.0.0.5",
            ...(mode === "default" ? {} : { relative_move: relativeMove }),
          },
        },
      },
    },
  });
  await frigateApp.page.route("**/api/front_door/ptz/info**", (route) =>
    route.fulfill({
      json: {
        name: "front_door",
        features,
        presets: [],
        preset_details: [],
        max_presets: null,
        profiles: [],
      },
    }),
  );
  await frigateApp.goto("/#front_door");
  // the pan buttons show once the PTZ info has loaded
  await expect(
    frigateApp.page.getByRole("button", { name: MOVE_LEFT }),
  ).toBeVisible();
}

test.describe("PTZ click to move @medium @mobile", () => {
  test("generic mode offers click to move without FOV moves", async ({
    frigateApp,
  }) => {
    await openLiveView(frigateApp, "generic", ["pt", "pt-r-generic"]);

    await expect(
      frigateApp.page.getByRole("button", { name: CLICK_TO_MOVE }),
    ).toBeVisible();
  });

  test("generic mode needs generic moves", async ({ frigateApp }) => {
    await openLiveView(frigateApp, "generic", ["pt", "pt-r-fov"]);

    await expect(
      frigateApp.page.getByRole("button", { name: CLICK_TO_MOVE }),
    ).toHaveCount(0);
  });

  test("default mode offers click to move with FOV moves", async ({
    frigateApp,
  }) => {
    await openLiveView(frigateApp, "default", ["pt", "pt-r-fov"]);

    await expect(
      frigateApp.page.getByRole("button", { name: CLICK_TO_MOVE }),
    ).toBeVisible();
  });

  test("fov mode still needs FOV moves", async ({ frigateApp }) => {
    await openLiveView(frigateApp, "fov", ["pt", "pt-r-generic"]);

    await expect(
      frigateApp.page.getByRole("button", { name: CLICK_TO_MOVE }),
    ).toHaveCount(0);
  });
});
