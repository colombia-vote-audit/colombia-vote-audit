import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useFetch } from "./common";
import { useI18n, type Lang } from "./i18n";
import { go, href, parse, replace, useRoute } from "./router";
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

  const listHash = (next: { mode?: string; q?: string; from?: string; to?: string }) => {
    const m = next.mode ?? mode;
    return m === "legislators"
      ? href("/legislators", { q: next.q ?? q })
      : href("/", { q: next.q ?? q, from: next.from ?? from, to: next.to ?? to });
  };
  const update = (next: Parameters<typeof listHash>[0]) =>
    onList ? replace(listHash(next)) : go(listHash(next));

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [route.path, detail?.[2]]);

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
        </div>
      )}

      <main>
        {detail?.[1] === "vote" && <VoteDetail id={Number(detail[2])} back={lastList.current} />}
        {detail?.[1] === "legislator" && <LegislatorDetail id={Number(detail[2])} back={lastList.current} />}
        {onList && mode === "votes" && <VoteList q={q} from={from} to={to} />}
        {onList && mode === "legislators" && <Legislators q={q} />}
      </main>
    </div>
  );
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
