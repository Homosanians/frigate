/**
 * Toast tests -- HIGH tier.
 *
 * Sonner renders every toast in every mounted <Toaster>, so the app mounts a
 * single one for all routes. A view that adds its own shows each toast twice,
 * and a route the shared one does not cover drops them.
 */

import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import { installFirstRun } from "../helpers/setup-wizard";
import { LivePage } from "../pages/live.page";

// one per mounted <Toaster>, present even while no toast is showing
function toasters(frigateApp: FrigateApp) {
  return frigateApp.page.locator('section[aria-label^="Notifications"]');
}

test.describe("Toasts - one toaster per page @high @mobile", () => {
  test("the live dashboard", async ({ frigateApp }) => {
    await frigateApp.goto("/");
    const live = new LivePage(frigateApp.page, !frigateApp.isMobile);
    await expect(live.cameraCard("front_door").first()).toBeVisible({
      timeout: 10_000,
    });
    await expect(toasters(frigateApp)).toHaveCount(1);
  });

  test("a camera group's draggable grid", async ({ frigateApp }) => {
    test.skip(frigateApp.isMobile, "Draggable grid is desktop-only");
    await frigateApp.goto("/?group=outdoor");
    const live = new LivePage(frigateApp.page, true);
    await expect(live.cameraCard("backyard").first()).toBeVisible({
      timeout: 10_000,
    });
    await expect(toasters(frigateApp)).toHaveCount(1);
  });

  test("a single camera", async ({ frigateApp }) => {
    test.skip(frigateApp.isMobile, "Back is an icon on mobile");
    await frigateApp.goto("/#front_door");
    const live = new LivePage(frigateApp.page, true);
    await expect(live.backButton).toBeVisible({ timeout: 10_000 });
    await expect(toasters(frigateApp)).toHaveCount(1);
  });

  test("chat", async ({ frigateApp }) => {
    await frigateApp.goto("/chat");
    await expect(frigateApp.page.getByPlaceholder(/ask/i)).toBeVisible({
      timeout: 10_000,
    });
    await expect(toasters(frigateApp)).toHaveCount(1);
  });

  test("the setup wizard", async ({ frigateApp }) => {
    await installFirstRun(frigateApp, frigateApp.page);
    await frigateApp.gotoAndWait("/", "text=Welcome to Frigate");
    await expect(toasters(frigateApp)).toHaveCount(1);
  });

  for (const path of ["/review", "/explore", "/settings", "/system#general"]) {
    test(path, async ({ frigateApp }) => {
      await frigateApp.goto(path);
      await expect(toasters(frigateApp)).toHaveCount(1);
    });
  }
});
