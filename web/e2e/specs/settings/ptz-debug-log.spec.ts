/**
 * PTZ debug log in the camera Debug view -- MEDIUM tier.
 *
 * The PTZ tab exists only for admins and only for cameras with ONVIF
 * configured, because the endpoint it polls requires the admin role. While it
 * is open it polls /api/<camera>/ptz/debug and shows the camera status and the
 * log rows the backend returns, asking only for newer entries after the first
 * poll.
 */

import { test, expect } from "../../fixtures/frigate-test";
import { viewerProfile } from "../../fixtures/mock-data/profile";
import {
  grantClipboardPermissions,
  readClipboard,
} from "../../helpers/clipboard";
import type { Page } from "@playwright/test";

const DEBUG_URL = "/?debug=true#front_door";

const STATUS = {
  pan_tilt: "IDLE",
  zoom: null,
  position: { pan: -0.2014, tilt: 1, zoom: null },
  time: 1760020351.05,
  error: null,
};

const SNAPSHOT = {
  session: "s1",
  connected: true,
  capabilities: {
    features: ["pt", "pt-r", "pt-r-fov"],
    relative_spaces: [
      { space: "generic", x: [-1, 1], y: [-1, 1] },
      { space: "fov", x: [-1, 1], y: [-1, 1] },
    ],
    default_relative_space: "generic",
  },
  status: STATUS,
  seq: 3,
  missed: false,
  entries: [
    {
      id: 1,
      seq: 1,
      time: 1760020349.12,
      source: "command",
      kind: "request",
      repeats: 1,
      until: null,
      data: {
        operation: "RelativeMove",
        space: "fov",
        pan: 0.7,
        tilt: -0.1,
        zoom: 0,
        x: 0.7,
        y: -0.1,
        speed: 1,
        duration_ms: 85,
        error: null,
      },
    },
    {
      id: 2,
      seq: 2,
      time: 1760020351.05,
      source: "debug",
      kind: "status",
      repeats: 3,
      until: 1760020352.05,
      data: {
        pan_tilt: "IDLE",
        zoom: null,
        position: STATUS.position,
        position_start: STATUS.position,
        error: null,
      },
    },
    {
      id: 3,
      seq: 3,
      time: 1760020352.5,
      source: "debug",
      kind: "status",
      repeats: 1,
      until: null,
      data: {
        pan_tilt: "MOVING",
        zoom: null,
        position: null,
        position_start: null,
        error: null,
      },
    },
  ],
};

async function mockPtzDebug(
  page: Page,
  snapshot: object = SNAPSHOT,
): Promise<string[]> {
  const afters: string[] = [];

  await page.route("**/api/front_door/ptz/debug**", (route) => {
    const after =
      new URL(route.request().url()).searchParams.get("after") ?? "0";
    afters.push(after);
    return route.fulfill({
      json: after === "0" ? snapshot : { ...snapshot, entries: [] },
    });
  });

  return afters;
}

test.describe("PTZ debug log @medium @mobile", () => {
  test("no PTZ tab for a camera without ONVIF", async ({ frigateApp }) => {
    await frigateApp.goto(DEBUG_URL);

    await expect(
      frigateApp.page.getByRole("tab", { name: "Debugging" }),
    ).toBeVisible();
    await expect(frigateApp.page.getByRole("tab", { name: "PTZ" })).toHaveCount(
      0,
    );
  });

  test("PTZ tab shows the camera status and the log", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      config: { cameras: { front_door: { onvif: { host: "10.0.0.5" } } } },
    });
    const afters = await mockPtzDebug(frigateApp.page);
    await frigateApp.goto(DEBUG_URL);

    await frigateApp.page.getByRole("tab", { name: "PTZ" }).click();

    const status = frigateApp.page.getByTestId("ptz-debug-status");
    await expect(status).toContainText("IDLE");
    await expect(status).toContainText("pan -0.201, tilt 1.000");
    await expect(status).toContainText(
      "generic (x -1.00 to 1.00, y -1.00 to 1.00), fov (x -1.00 to 1.00, y -1.00 to 1.00)",
    );

    const rows = frigateApp.page.getByTestId("ptz-debug-row");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("RelativeMove");
    await expect(rows.nth(0)).toContainText("85 ms");
    await expect(rows.nth(1)).toContainText("repeated 3 times");
    await expect(rows.nth(2)).toContainText("MOVING");

    await rows.nth(0).click();
    await expect(rows.nth(0)).toContainText('"operation": "RelativeMove"');

    // later polls ask only for entries newer than the last one seen
    await expect
      .poll(() => afters.length, { timeout: 5_000 })
      .toBeGreaterThan(1);
    expect(afters.slice(1)).toContain("3");
  });

  test("Copy puts the status and every row on the clipboard", async ({
    frigateApp,
  }) => {
    await grantClipboardPermissions(frigateApp.page.context());
    await frigateApp.installDefaults({
      config: { cameras: { front_door: { onvif: { host: "10.0.0.5" } } } },
    });
    await mockPtzDebug(frigateApp.page);
    await frigateApp.goto(DEBUG_URL);

    await frigateApp.page.getByRole("tab", { name: "PTZ" }).click();
    await expect(frigateApp.page.getByTestId("ptz-debug-row")).toHaveCount(3);

    await frigateApp.page.getByRole("button", { name: "Copy" }).click();

    await expect
      .poll(() => readClipboard(frigateApp.page))
      .toContain("front_door");
    const text = await readClipboard(frigateApp.page);
    expect(text).toContain("pan -0.201, tilt 1.000");
    // the summary line, then the full entry for pasting into an issue
    expect(text).toContain("RelativeMove in fov space");
    expect(text).toContain('"operation":"RelativeMove"');
    expect(text).toContain("repeated 3 times");
  });

  test("no PTZ tab for a viewer, who cannot read the log", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      config: { cameras: { front_door: { onvif: { host: "10.0.0.5" } } } },
      profile: viewerProfile(),
    });
    const afters = await mockPtzDebug(frigateApp.page);
    await frigateApp.goto(DEBUG_URL);

    await expect(
      frigateApp.page.getByRole("tab", { name: "Debugging" }),
    ).toBeVisible();
    await expect(frigateApp.page.getByRole("tab", { name: "PTZ" })).toHaveCount(
      0,
    );
    expect(afters).toEqual([]);
  });

  test("a failed calibration stands out from a finished one", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      config: { cameras: { front_door: { onvif: { host: "10.0.0.5" } } } },
    });
    const calibration = (id: number, data: Record<string, unknown>) => ({
      id,
      seq: id,
      time: 1760020349 + id,
      source: "calibration",
      kind: "calibration",
      repeats: 1,
      until: null,
      data,
    });
    await mockPtzDebug(frigateApp.page, {
      ...SNAPSHOT,
      entries: [
        calibration(1, { event: "finished", stream_latency: 0.5 }),
        calibration(2, { event: "failed", reason: "busy" }),
      ],
    });
    await frigateApp.goto(DEBUG_URL);

    await frigateApp.page.getByRole("tab", { name: "PTZ" }).click();

    const rows = frigateApp.page.getByTestId("ptz-debug-row");
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText("Calibration finished");
    await expect(rows.nth(0).locator(".text-destructive")).toHaveCount(0);
    await expect(rows.nth(1)).toContainText("Calibration stopped");
    await expect(rows.nth(1).locator(".text-destructive")).toHaveCount(1);
  });
});
