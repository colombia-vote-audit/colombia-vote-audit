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
  window.dispatchEvent(new Event(CHANGE));
}

export function go(hash: string) {
  location.hash = hash;
}
