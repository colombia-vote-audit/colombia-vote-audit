import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useFetch } from "./common";
import { useI18n, type Lang } from "./i18n";
import { go, href, parse, previous, replace, useRoute, type Route } from "./router";
import { LegislatorDetail } from "./views/LegislatorDetail";
import { Legislators } from "./views/Legislators";
import { VoteDetail } from "./views/VoteDetail";
import { VoteList } from "./views/VoteList";

export function App() {
  const { t, num, lang, setLang } = useI18n();
  const route = useRoute();
  const stats = useFetch(() => api.stats(), []);

  const detail = route.path.match(/^\/(vote|legislator)\/(\d+)$/);
  const onList = !detail;
  // The last list page, so detail pages can link back to the same search.
  const lastList = useRef("#/");
  if (onList) lastList.current = location.hash || "#/";
  const listRoute = onList ? route : parse(lastList.current);
  const mode = listRoute.path === "/legislators" ? "legislators" : "votes";
  const q = listRoute.params.get("q") ?? "";
  const from = listRoute.params.get("from") ?? "";
  const to = listRoute.params.get("to") ?? "";
  const chamber = listRoute.params.get("chamber") ?? "";
  const backHash = previous() ?? lastList.current;
  const back = { href: backHash, label: backLabel(parse(backHash), t) };

  const listHash = (next: { mode?: string; q?: string; from?: string; to?: string; chamber?: string }) => {
    const m = next.mode ?? mode;
    return m === "legislators"
      ? href("/legislators", { q: next.q ?? q })
      : href("/", {
          q: next.q ?? q,
          from: next.from ?? from,
          to: next.to ?? to,
          chamber: next.chamber ?? chamber,
        });
  };
  const update = (next: Parameters<typeof listHash>[0]) =>
    onList ? replace(listHash(next)) : go(listHash(next));

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [route.path, detail?.[2]]);
  // Detail pages set their own title once they've loaded.
  useEffect(() => {
    if (onList) document.title = t.title;
  }, [onList, t]);

  return (
    <div className="page">
      <header className="top">
        <a className="brand" href="#/">
          <span>{t.title}</span>
          <small>{t.tagline}</small>
        </a>
        <div className="top-right">
          {stats.data && (
            <small>{t.stats(num(stats.data.votes), num(stats.data.records), num(stats.data.legislators))}</small>
          )}
          <div className="lang" role="group" aria-label="Language">
            {(["en", "es"] as Lang[]).map((l) => (
              <button key={l} className={l === lang ? "on" : ""} onClick={() => setLang(l)}>
                {l.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
      </header>

      <div className="search">
        <SearchInput
          value={q}
          placeholder={mode === "votes" ? t.searchVotes : t.searchLegislators}
          onChange={(v) => update({ q: v })}
        />
        <div className="toggle" role="group">
          <button className={mode === "votes" ? "on" : ""} onClick={() => go(listHash({ mode: "votes", q: "" }))}>
            {t.votes}
          </button>
          <button
            className={mode === "legislators" ? "on" : ""}
            onClick={() => go(listHash({ mode: "legislators", q: "" }))}
          >
            {t.legislators}
          </button>
        </div>
      </div>
      {mode === "votes" && (
        <div className="dates">
          <label>
            {t.from} <input type="date" value={from} onChange={(e) => update({ from: e.target.value })} />
          </label>
          <label>
            {t.to} <input type="date" value={to} onChange={(e) => update({ to: e.target.value })} />
          </label>
          {(from || to) && (
            <button className="link" onClick={() => update({ from: "", to: "" })}>
              {t.clearDates}
            </button>
          )}
          <select value={chamber} onChange={(e) => update({ chamber: e.target.value })}>
            <option value="">{t.bothChambers}</option>
            <option value="Cámara">Cámara</option>
            <option value="Senado">Senado</option>
          </select>
        </div>
      )}

      <main>
        {detail?.[1] === "vote" && <VoteDetail id={Number(detail[2])} back={back} />}
        {detail?.[1] === "legislator" && <LegislatorDetail id={Number(detail[2])} back={back} />}
        {onList && mode === "votes" && <VoteList q={q} from={from} to={to} chamber={chamber} />}
        {onList && mode === "legislators" && <Legislators q={q} />}
      </main>

      <footer className="foot">
        <a href="/download-db" download>
          {t.downloadDb}
        </a>
        {stats.data && (
          <small>
            {t.downloadNote(
              num(Math.round(stats.data.download_bytes / 1e6)),
              num(Math.round(stats.data.database_bytes / 1e6)),
            )}
          </small>
        )}
      </footer>
    </div>
  );
}

function backLabel(route: Route, t: ReturnType<typeof useI18n>["t"]): string {
  if (route.path.startsWith("/vote/")) return t.backToVote;
  if (route.path.startsWith("/legislator/")) return t.backToLegislator;
  if (route.path === "/legislators") return t.allLegislators;
  return route.params.size ? t.backToSearch : t.allVotes;
}

// Updates the URL a moment after typing stops.
function SearchInput({ value, placeholder, onChange }: { value: string; placeholder: string; onChange: (v: string) => void }) {
  const [text, setText] = useState(value);
  useEffect(() => {
    setText(value);
  }, [value]);
  useEffect(() => {
    if (text === value) return;
    const timer = setTimeout(() => onChange(text), 250);
    return () => clearTimeout(timer);
  }, [text]);
  return <input type="search" value={text} placeholder={placeholder} onChange={(e) => setText(e.target.value)} />;
}
