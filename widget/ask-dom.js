// Small helpers the question box's modules share: building elements, and the
// messages shown when the API refuses or can't be reached.

export const UNREACHABLE = "Could not reach the question service. Please check your connection and try again.";

export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

// A button that looks like a link.
export function link(text, onclick) {
  return el("button", { type: "button", class: "mt-ask-link", onclick }, text);
}

export function errorMessage(status, body, retryAfter) {
  const message = body?.error?.message;
  if (status === 429 && retryAfter) {
    const minutes = Math.ceil(Number(retryAfter) / 60);
    const wait = minutes >= 60 ? `${Math.ceil(minutes / 60)} hours` : `${minutes} minute${minutes === 1 ? "" : "s"}`;
    return `${message ?? "Too many questions."} You can ask again in about ${wait}.`;
  }
  return message ?? "Something went wrong. Please try again later.";
}
