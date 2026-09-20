import { test, expect } from "@playwright/test";

/**
 * User: "My website is missing the landing page just blank login page.
 * Improve the login page and follow the indian data security reules,
 * plicies and add all those give my mail for contact." Previously
 * App.tsx's `session === null` branch rendered `<Login />` directly, with
 * nothing in front of it. Now it renders `<PreAuth />`
 * (web/src/auth/PreAuth.tsx), which adds a real landing page plus Privacy
 * Policy / Terms of Service pages (written for India's DPDPA + IT Rules),
 * all sharing one header/footer (PublicShell.tsx) with the sign-in CTA and
 * the contact email.
 */

test("unauthenticated visitor sees a landing page, can reach sign-in and the legal pages", async ({ page }) => {
  await page.goto("/");

  // landing, not a blank login form
  await expect(
    page.getByRole("heading", { name: /Support automation that knows when to ask for help/ }),
  ).toBeVisible();
  await expect(page.getByText("Build flows without writing code")).toBeVisible();

  // hero CTA reaches the real sign-in form
  await page.getByRole("button", { name: "Sign in to get started" }).click();
  await expect(page.getByText("Continue with Google")).toBeVisible();
  await expect(page.getByPlaceholder("you@company.com")).toBeVisible();

  // header nav returns to the landing page
  await page.getByRole("button", { name: "← Home" }).click();
  await expect(page.getByText("Build flows without writing code")).toBeVisible();

  // footer -> Privacy Policy names the actual Indian data-protection law
  // and the contact email, not filler text
  await page.getByRole("button", { name: "Privacy Policy" }).click();
  await expect(page.getByRole("heading", { name: "Privacy Policy" })).toBeVisible();
  await expect(page.getByText(/Digital Personal Data Protection Act/)).toBeVisible();
  await expect(page.locator('a[href="mailto:gundamvishnu7@gmail.com"]').first()).toBeVisible();

  // footer -> Terms of Service
  await page.getByRole("button", { name: "Terms of Service" }).click();
  await expect(page.getByRole("heading", { name: "Terms of Service" })).toBeVisible();

  // footer -> Cookie Policy (no GTM/GA4 env vars set in this test run, so
  // no consent banner -- this only checks the page itself is reachable)
  await page.getByRole("button", { name: "Cookie Policy" }).click();
  await expect(page.getByRole("heading", { name: "Cookie Policy" })).toBeVisible();
  await expect(page.locator(".cookie-banner")).toHaveCount(0);
});

test("landing page has no horizontal overflow at phone width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: /Support automation that knows when to ask for help/ }),
  ).toBeVisible();
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  const clientWidth = await page.evaluate(() => document.documentElement.clientWidth);
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 1);
});
