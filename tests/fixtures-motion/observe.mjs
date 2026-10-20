export default async (page, mark) => {
  if (await page.locator('#box[data-hover]').count()) {
    await mark('saturation-hover');
    await page.locator('#box').hover();
    await page.waitForTimeout(750);
  }
};
