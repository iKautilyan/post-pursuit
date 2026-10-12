from playwright.sync_api import sync_playwright


JOB_URL = "https://de.linkedin.com/jobs/view/frontend-developer-in-at-hdi-group-4445209228"


def inspect_job_detail():

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        page = browser.new_page(
            viewport={
                "width": 1440,
                "height": 900,
            }
        )

        print("Opening job page...")

        page.goto(
            JOB_URL,
            wait_until="domcontentloaded",
        )

        page.wait_for_timeout(5000)

        print("\nPAGE TITLE:")
        print(page.title())

        print("\nPAGE URL:")
        print(page.url)

        print("\nTEXT CONTAINING DESCRIPTION:\n")

        # Look for elements commonly used for the job description.
        selectors = [
            ".show-more-less-html__markup",
            ".description__text",
            "[class*='description']",
        ]

        for selector in selectors:

            elements = page.locator(selector)

            print(
                f"\nSelector: {selector}"
            )

            print(
                f"Elements found: {elements.count()}"
            )

            for i in range(
                min(elements.count(), 3)
            ):

                try:
                    text = (
                        elements
                        .nth(i)
                        .inner_text()
                        .strip()
                    )

                    print(
                        f"\n--- Element {i} ---"
                    )

                    print(
                        text[:5000]
                    )

                except Exception as e:

                    print(
                        f"Error: {e}"
                    )

        input(
            "\nPress Enter to close..."
        )

        browser.close()


if __name__ == "__main__":
    inspect_job_detail()
