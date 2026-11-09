// What the box says about its data: the labels the site tracks, above the form, and
// the "About the data" section, with the dates and the label count from the API.

import { el } from "./ask-dom.js";

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];

// "2026-11-02" -> "2 November 2026", the same in every browser language.
export function longDate(day) {
  const [year, month, date] = String(day).split("-").map(Number);
  return MONTHS[month - 1] && date ? `${date} ${MONTHS[month - 1]} ${year}` : String(day);
}

// request(path) fetches from the API. Returns the two elements and a loader for each.
export function createAbout({ request }) {
  // What the numbers mean. The dates and the label count are added from the API when they load.
  const aboutFacts = el("ul", { class: "mt-ask-about-facts" }, el("li", {}, "Source: Discogs"));
  const about = el("details", { class: "mt-ask-about" },
    el("summary", {}, "About the data"),
    el("p", {}, "A price is the cheapest copy for sale on Discogs, in any condition, without shipping, "
      + "converted to euros by Discogs. It is an asking price, not what a record sold for."),
    el("p", {}, "Prices are updated every day. Wants, haves and ratings are updated once a week."),
    aboutFacts,
  );
  // Filled in from the API, and stays hidden if the list can't be loaded.
  const labels = el("aside", { class: "mt-ask-labels", "aria-label": "Labels this site tracks", hidden: "" });

  async function loadLabels() {
    try {
      const response = await request("/api/labels");
      const names = response.ok ? (await response.json()).labels : [];
      if (!names?.length) return;
      labels.replaceChildren(
        el("strong", {}, `Labels we track (${names.length}): `),
        names.join(", "),
      );
      labels.hidden = false;
      aboutFacts.append(el("li", {}, `Labels: ${names.length}`));
    } catch {
      // The box works without the list.
    }
  }

  async function loadDates() {
    try {
      const response = await request("/api/data-dates");
      const dates = response.ok ? await response.json() : {};
      const facts = [["Tracked since", dates.tracked_since], ["Last updated", dates.updated]]
        .filter(([, day]) => day)
        .map(([name, day]) => el("li", {}, `${name}: ${longDate(day)}`));
      aboutFacts.firstChild.after(...facts);
    } catch {
      // The section still says what the data is.
    }
  }

  return { labels, about, loadLabels, loadDates };
}
