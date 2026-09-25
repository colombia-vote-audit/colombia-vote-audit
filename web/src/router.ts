import { useEffect, useState } from "react";

// Routes live in the hash (#/vote/12, #/?q=renuncia) so the API server only
// has to serve index.html at /.

export interface Route {
  path: string;
  params: URLSearchParams;
}

const CHANGE = "cva:route";

export function parse(hash: string): Route {
  const [path, query = ""] = (hash.slice(1) || "/").split("?");
  return { path, params: new URLSearchParams(query) };
}

const current = () => parse(location.hash);

// Pages visited in this tab, so a detail page can link back to wherever the
// reader came from. Going to the page just below the top counts as going back.
const visited: string[] = [location.hash || "#/"];

function track() {
  const hash = location.hash || "#/";
  if (visited.length > 1 && visited[visited.length - 2] === hash) visited.pop();
  else if (visited[visited.length - 1] !== hash) visited.push(hash);
}
window.addEventListener("hashchange", track);

// The previous page in the app, or null when the reader arrived here directly.
export function previous(): string | null {
  return visited.length > 1 ? visited[visited.length - 2] : null;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(current);
  useEffect(() => {
    const update = () => setRoute(current());
    window.addEventListener("hashchange", update);
    window.addEventListener(CHANGE, update);
    return () => {
      window.removeEventListener("hashchange", update);
      window.removeEventListener(CHANGE, update);
    };
  }, []);
  return route;
}

export function href(path: string, params: Record<string, string | undefined> = {}): string {
  const qs = new URLSearchParams(Object.entries(params).filter((e): e is [string, string] => !!e[1]));
  return `#${path}${qs.size ? `?${qs}` : ""}`;
}

// Replace the current entry, for search input, so typing doesn't fill the history.
export function replace(hash: string) {
  history.replaceState(null, "", hash);
  visited[visited.length - 1] = hash;
  window.dispatchEvent(new Event(CHANGE));
}

export function go(hash: string) {
  location.hash = hash;
}
