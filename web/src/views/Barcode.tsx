import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, type AskReply, type BarcodeCol, type BarcodeRow, type Turn } from "../api";
import {
  agreement,
  closeness,
  COLORS,
  columns,
  PARTY_COLORS,
  prepare,
  referenceParties,
  ROW_HEIGHT,
  viewFromParams,
  viewParams,
  VOTE_TYPES,
  winner,
  withDefaults,
  type Grid,
  type View,
} from "../barcode";
import { Status, useFetch } from "../common";
import { useI18n } from "../i18n";
import { href, replace, useRoute } from "../router";

type T = ReturnType<typeof useI18n>["t"];

const partyColor = (p: string) => PARTY_COLORS[p] ?? PARTY_COLORS["Other parties"];

export function Barcode() {
  const { t } = useI18n();
  const data = useFetch(() => api.barcode(), []);
  const grid = useMemo(() => (data.data ? prepare(data.data) : null), [data.data]);
  useEffect(() => {
    document.title = `${t.bc.title} · ${t.title}`;
  }, [t]);
  return (
    <div className="barcode">
      <h1>{t.bc.title}</h1>
      <p className="bc-intro">{t.bc.intro}</p>
      <Status loading={data.loading && !grid} error={data.error}>
        {grid && <Page grid={grid} />}
      </Status>
    </div>
  );
}

function Page({ grid }: { grid: Grid }) {
  const { t } = useI18n();
  const route = useRoute();
  const view = viewFromParams(route.params);
  const show = (v: View) => replace(href("/barcode", viewParams(v)));
  const setView = (next: Partial<View>) => show({ ...view, ...next });
  const cols = useMemo(
    () => columns(grid, view),
    [grid, view.q, view.min, view.types.join(), view.from, view.to, view.order, view.a],
  );
  return (
    <>
      {grid.ask && <Ask grid={grid} view={view} onView={show} />}
      <Controls grid={grid} view={view} setView={setView} shown={cols.length} />
      <Board grid={grid} view={view} cols={cols} setView={setView} />
      <VoteCard grid={grid} view={view} cols={cols} />
      <p className="bc-note muted">{t.bc.readMe}</p>
    </>
  );
}

// ---------- controls ----------

function Seg<V extends string>({ value, options, onChange }: { value: V; options: [V, string][]; onChange: (v: V) => void }) {
  return (
    <div className="toggle bc-seg" role="group">
      {options.map(([v, label]) => (
        <button key={v} className={v === value ? "on" : ""} onClick={() => onChange(v)}>
          {label}
        </button>
      ))}
    </div>
  );
}

function PartySelect({ label, value, parties, onChange }: { label: string; value: string; parties: string[]; onChange: (p: string) => void }) {
  const { t } = useI18n();
  return (
    <label className="bc-field inline">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)} style={{ borderLeftColor: partyColor(value) }}>
        {parties.map((p) => (
          <option key={p} value={p}>
            {t.bc.party(p)}
          </option>
        ))}
      </select>
    </label>
  );
}

function Controls({ grid, view, setView, shown }: { grid: Grid; view: View; setView: (v: Partial<View>) => void; shown: number }) {
  const { t, num } = useI18n();
  const refs = useMemo(() => referenceParties(grid), [grid]);
  // The slider and the topic box update the URL once input pauses.
  const [topic, setTopic] = useState(view.q);
  const [min, setMin] = useState(view.min);
  useEffect(() => setTopic(view.q), [view.q]);
  useEffect(() => setMin(view.min), [view.min]);
  useEffect(() => {
    if (topic === view.q && min === view.min) return;
    const timer = setTimeout(() => setView({ q: topic, min, col: null }), 200);
    return () => clearTimeout(timer);
  }, [topic, min]);
  const [pin, setPin] = useState("");
  const addPin = (name: string) => {
    const q = name.trim().toLowerCase();
    const r = grid.rows.find((r) => r.name.toLowerCase() === q) ?? grid.rows.find((r) => r.name.toLowerCase().includes(q));
    if (r && !view.pins.includes(r.id)) setView({ pins: [...view.pins, r.id] });
    setPin("");
  };
  const usesA = ["ab", "with"].includes(view.color) || view.order === "a" || view.sort === "align";

  return (
    <div className="bc-controls">
      <fieldset>
        <legend>{t.bc.colorBy}</legend>
        <Seg value={view.color} onChange={(color) => setView({ color })} options={COLORS.map((c) => [c, t.bc.color[c]])} />
        {usesA && (
          <div className="bc-row">
            <PartySelect label={t.bc.partyA} value={view.a} parties={refs} onChange={(a) => setView({ a, b: a === view.b ? view.a : view.b })} />
            {view.color === "ab" && <PartySelect label={t.bc.partyB} value={view.b} parties={refs.filter((p) => p !== view.a)} onChange={(b) => setView({ b })} />}
          </div>
        )}
        <Key view={view} />
      </fieldset>

      <fieldset>
        <legend>{t.bc.whichVotes}</legend>
        <label className="bc-slider">
          <span>{min === 0 ? t.bc.everyVote : t.bc.threshold(min)}</span>
          <input type="range" min={0} max={50} step={1} value={min} onChange={(e) => setMin(Number(e.target.value))} />
          <span className="mono small muted">{t.bc.shown(num(shown), num(grid.cols.length))}</span>
        </label>
        <div className="chips">
          <span className="muted small">{t.bc.voteTypes}</span>
          {VOTE_TYPES.map((vt) => (
            <button
              key={vt}
              className={`chip${view.types.includes(vt) ? " on" : ""}`}
              onClick={() => setView({ types: view.types.includes(vt) ? view.types.filter((x) => x !== vt) : [...view.types, vt], col: null })}
            >
              {t.voteType[vt]}
            </button>
          ))}
        </div>
        <div className="bc-row">
          <label className="bc-field">
            {t.bc.topic}
            <input type="search" value={topic} placeholder={t.bc.topicPlaceholder} onChange={(e) => setTopic(e.target.value)} />
          </label>
          <div className="chips">
            {t.bc.topics.map(([label, q]) => (
              <button key={q} className={`chip${view.q === q ? " on" : ""}`} onClick={() => setView({ q: view.q === q ? "" : q, col: null })}>
                {label}
              </button>
            ))}
          </div>
        </div>
        {(view.from || view.to) && (
          <p className="bc-zoom">
            {t.bc.zoomed(view.from, view.to)}{" "}
            <button className="link" onClick={() => setView({ from: "", to: "" })}>
              {t.bc.resetZoom}
            </button>
          </p>
        )}
      </fieldset>

      <fieldset>
        <legend>{t.bc.arrange}</legend>
        <div className="bc-row">
          <label className="bc-field inline">
            {t.bc.columns}
            <Seg value={view.order} onChange={(order) => setView({ order })} options={[["date", t.bc.byDate], ["a", t.bc.byA], ["margin", t.bc.byMargin]]} />
          </label>
          <label className="bc-field inline">
            {t.bc.rowsLabel}
            <Seg value={view.sort} onChange={(sort) => setView({ sort })} options={[["party", t.bc.byParty], ["align", t.bc.byAgreement]]} />
          </label>
          <Seg value={view.rows} onChange={(rows) => setView({ rows })} options={[["compact", t.bc.rows.compact], ["tall", t.bc.rows.tall], ["named", t.bc.rows.named]]} />
        </div>
        <div className="bc-row">
          <label className="bc-field">
            {t.bc.pin}
            <input
              list="bc-names"
              value={pin}
              placeholder={t.bc.pinPlaceholder}
              onChange={(e) => {
                setPin(e.target.value);
                if (grid.rows.some((r) => r.name === e.target.value)) addPin(e.target.value);
              }}
              onKeyDown={(e) => e.key === "Enter" && addPin(pin)}
            />
            <datalist id="bc-names">
              {grid.rows.map((r) => (
                <option key={r.id} value={r.name} />
              ))}
            </datalist>
          </label>
          <div className="chips">
            {view.pins.map((id) => grid.rowById.get(id)).filter((r): r is BarcodeRow => !!r).map((r) => (
              <button key={r.id} className="chip" style={{ ["--c" as string]: partyColor(r.party) }} onClick={() => setView({ pins: view.pins.filter((p) => p !== r.id) })} aria-label={`${t.bc.unpin} ${r.name}`}>
                <i /> {r.name} <span className="x">×</span>
              </button>
            ))}
          </div>
        </div>
        <div className="chips">
          <span className="muted small">{t.bc.showOnly}</span>
          {grid.parties.map((p) => (
            <button
              key={p}
              className={`chip${view.parties.includes(p) ? " on" : ""}`}
              style={{ ["--c" as string]: partyColor(p) }}
              onClick={() => setView({ parties: view.parties.includes(p) ? view.parties.filter((x) => x !== p) : [...view.parties, p] })}
            >
              <i /> {t.bc.party(p)}
            </button>
          ))}
        </div>
      </fieldset>
    </div>
  );
}

function Key({ view }: { view: View }) {
  const { t } = useI18n();
  return (
    <div className="bc-key">
      {t.bc.key(view.color, t.bc.party(view.a), t.bc.party(view.b)).map(([cls, label]) => (
        <span key={cls}>
          <i className={`sw ${cls}`} />
          {label}
        </span>
      ))}
      <span>
        <i className="sw off" />
        {t.bc.notInChamber}
      </span>
    </div>
  );
}

// ---------- the grid ----------

interface Line {
  r: BarcodeRow;
  y: number;
  h: number;
}

interface Label {
  top: number;
  height: number;
  text: string;
  color: string;
  one?: boolean;
}

function layout(grid: Grid, view: View, cols: BarcodeCol[]) {
  const lines: Line[] = [];
  const labels: Label[] = [];
  const h = ROW_HEIGHT[view.rows];
  const named = view.rows === "named";
  let y = 0;
  const pinned = view.pins.map((id) => grid.rowById.get(id)).filter((r): r is BarcodeRow => !!r);
  for (const r of pinned) {
    labels.push({ top: y, height: 12, text: r.name, color: partyColor(r.party), one: true });
    lines.push({ r, y, h: 12 });
    y += 14;
  }
  if (pinned.length) y += 10;
  let rows = view.parties.length ? grid.rows.filter((r) => view.parties.includes(r.party)) : grid.rows;
  if (view.sort === "align") {
    // Everyone in one list, most in agreement with party A first.
    const score = new Map(rows.map((r) => [r.id, agreement(grid, r, cols, view.a) ?? -1]));
    rows = [...rows].sort((a, b) => score.get(b.id)! - score.get(a.id)!);
    for (const r of rows) {
      labels.push({ top: y, height: h - (h > 4 ? 1 : 0), text: named ? r.name : "", color: partyColor(r.party), one: true });
      lines.push({ r, y, h });
      y += h;
    }
    return { lines, labels, height: Math.max(y, 20) };
  }
  let prev: string | null = null;
  let group: Label | null = null;
  for (const r of rows) {
    if (r.party !== prev) {
      if (prev) y += named ? 10 : 5;
      prev = r.party;
      group = { top: y, height: 0, text: r.party, color: partyColor(r.party) };
      if (!named) labels.push(group);
    }
    if (named) labels.push({ top: y, height: h - 1, text: r.name, color: partyColor(r.party), one: true });
    lines.push({ r, y, h });
    y += h;
    group!.height += h;
  }
  return { lines, labels, height: Math.max(y, 20) };
}

const CELL_VARS = ["--yes", "--no", "--abstain", "--skip", "--away", "--off", "--text", "--left", "--right", "--both"] as const;

function Board({ grid, view, cols, setView }: { grid: Grid; view: View; cols: BarcodeCol[]; setView: (v: Partial<View>) => void }) {
  const { t, longDate } = useI18n();
  const { lines, labels, height } = useMemo(
    () => layout(grid, view, cols),
    [grid, cols, view.pins.join(), view.parties.join(), view.rows, view.sort, view.a],
  );
  const canvas = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<{ k: number; line: Line; x: number; y: number } | null>(null);
  const [brush, setBrush] = useState<{ x0: number; x1: number } | null>(null);
  const [width, setWidth] = useState(0);

  // Measure now, not only when the observer first reports, which can come late.
  useLayoutEffect(() => {
    const el = canvas.current!.parentElement!;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // A short fade when what the grid shows changes, not on hover.
  const shown = `${cols.length}|${cols[0]?.id}|${view.color}|${view.a}|${view.b}|${view.sort}|${view.parties}|${view.pins}|${view.rows}`;
  const lastShown = useRef(shown);
  useEffect(() => {
    if (lastShown.current === shown) return;
    lastShown.current = shown;
    if (!matchMedia("(prefers-reduced-motion: reduce)").matches)
      canvas.current?.animate([{ opacity: 0.25 }, { opacity: 1 }], { duration: 350, easing: "ease-out" });
  }, [shown]);

  useEffect(() => {
    const cv = canvas.current;
    if (!cv || !width) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(width * dpr);
    cv.height = Math.round(height * dpr);
    const ctx = cv.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!cols.length) return;
    const css = getComputedStyle(cv);
    const [yes, no, abst, skip, away, off, text, left, right, both] = CELL_VARS.map((v) => css.getPropertyValue(v).trim());
    const cw = width / cols.length;
    const gap = cw > 5 ? 1 : 0;
    const js = cols.map((c) => grid.index.get(c.id)!);
    const wins = cols.map(winner);
    const color = (v: string, k: number, r: BarcodeRow) => {
      if (v === ".") return off;
      if (view.color === "attend") return v === "a" ? no : v === "s" ? abst : away;
      if (v === "s") return skip;
      if (v === "a") return away;
      const maj = grid.majority[js[k]];
      switch (view.color) {
        case "vote":
          return v === "y" ? yes : v === "n" ? no : abst;
        case "outcome":
          return v === wins[k] ? yes : no;
        case "party": {
          const m = maj[r.party];
          return !m ? skip : v === m ? yes : no;
        }
        case "with": {
          const m = maj[view.a];
          return !m ? skip : v === m ? yes : no;
        }
        default: {
          const a = maj[view.a];
          const b = maj[view.b];
          if (!a || !b) return skip;
          if (a === b) return both;
          return v === a ? left : v === b ? right : abst;
        }
      }
    };
    for (const { r, y, h } of lines) {
      for (let k = 0; k < cols.length; k++) {
        ctx.fillStyle = color(r.cells[js[k]], k, r);
        ctx.fillRect(Math.floor(k * cw), y, Math.max(1, Math.ceil(cw) - gap), h - 1);
      }
    }
    const picked = cols.findIndex((c) => c.id === view.col);
    if (picked >= 0) {
      ctx.strokeStyle = text;
      ctx.lineWidth = 2;
      ctx.strokeRect(picked * cw - 1.5, 0, Math.max(cw, 1) + 3, height);
    }
    if (hover) {
      ctx.fillStyle = "rgba(128,128,128,.3)";
      ctx.fillRect(0, hover.line.y, width, hover.line.h - 1);
      ctx.fillRect(Math.floor(hover.k * cw), 0, Math.max(1, Math.ceil(cw)), height);
    }
  }, [cols, lines, height, width, view.color, view.a, view.b, view.col, hover]);

  const at = (clientX: number, clientY: number) => {
    const b = canvas.current!.getBoundingClientRect();
    const k = Math.floor((clientX - b.left) / (b.width / cols.length));
    const y = clientY - b.top;
    const line = lines.find((l) => y >= l.y && y < l.y + l.h);
    return k >= 0 && k < cols.length && line ? { k, line } : null;
  };

  const onUp = (e: React.PointerEvent) => {
    const d = brush;
    setBrush(null);
    if (d && view.order === "date" && Math.abs(d.x1 - d.x0) > 6) {
      const b = canvas.current!.getBoundingClientRect();
      const cw = b.width / cols.length;
      const k0 = Math.max(0, Math.floor((Math.min(d.x0, d.x1) - b.left) / cw));
      const k1 = Math.min(cols.length - 1, Math.floor((Math.max(d.x0, d.x1) - b.left) / cw));
      const sel = cols.slice(k0, k1 + 1);
      if (sel.length) setView({ from: sel[0].date, to: sel[sel.length - 1].date });
      return;
    }
    const h = at(e.clientX, e.clientY);
    if (h) setView({ col: cols[h.k].id });
  };

  const box = canvas.current?.getBoundingClientRect();
  const first = cols.length ? cols.reduce((a, c) => (c.date < a ? c.date : a), cols[0].date) : "";
  const last = cols.length ? cols.reduce((a, c) => (c.date > a ? c.date : a), cols[0].date) : "";

  return (
    <section className="bc-board">
      <div className="bc-frame">
        <div className="bc-labels" style={{ height }}>
          {labels.map((l, i) => (
            <div key={i} className={l.one ? "one" : ""} style={{ top: l.top, height: l.height, ["--c" as string]: l.color }} title={l.text}>
              {l.one ? l.text : l.height >= 12 ? t.bc.party(l.text) : ""}
            </div>
          ))}
        </div>
        <div className="bc-canvas">
          <canvas
            ref={canvas}
            style={{ height }}
            aria-label={t.bc.gridLabel}
            onPointerDown={(e) => {
              (e.target as HTMLElement).setPointerCapture(e.pointerId);
              setBrush({ x0: e.clientX, x1: e.clientX });
            }}
            onPointerMove={(e) => {
              if (brush) setBrush({ ...brush, x1: e.clientX });
              const h = at(e.clientX, e.clientY);
              setHover(h && !brush ? { ...h, x: e.clientX, y: e.clientY } : null);
            }}
            onPointerUp={onUp}
            onPointerLeave={() => setHover(null)}
          />
          {brush && box && view.order === "date" && Math.abs(brush.x1 - brush.x0) > 6 && (
            <div className="bc-brush" style={{ left: Math.min(brush.x0, brush.x1) - box.left, width: Math.abs(brush.x1 - brush.x0) }} />
          )}
        </div>
      </div>
      <div className="bc-axis">
        {!cols.length ? (
          <span>{t.bc.noMatch}</span>
        ) : view.order === "date" ? (
          <>
            <span>{longDate(first)}</span>
            <span>{t.bc.axis(cols.length)}</span>
            <span>{longDate(last)}</span>
          </>
        ) : view.order === "a" ? (
          <>
            <span>{t.bc.aYes(t.bc.party(view.a))}</span>
            <span>{t.bc.rollCalls(cols.length)}</span>
            <span>{t.bc.aNo(t.bc.party(view.a))}</span>
          </>
        ) : (
          <>
            <span>{t.bc.closest}</span>
            <span>{t.bc.rollCalls(cols.length)}</span>
            <span>{t.bc.widest}</span>
          </>
        )}
      </div>
      {hover && <Tip grid={grid} view={view} col={cols[hover.k]} row={hover.line.r} x={hover.x} y={hover.y} />}
    </section>
  );
}

function Tip({ grid, view, col, row, x, y }: { grid: Grid; view: View; col: BarcodeCol; row: BarcodeRow; x: number; y: number }) {
  const { t, longDate } = useI18n();
  const j = grid.index.get(col.id)!;
  const word = (c: string | null | undefined) => (c ? t.bc.cell[c as keyof T["bc"]["cell"]] : t.bc.split).toLowerCase();
  const refs = [...new Set([row.party, view.a, view.b])];
  return (
    <div className="bc-tip" style={{ left: Math.min(x + 14, window.innerWidth - 340), top: y + 14 }}>
      <b>{row.name}</b> · {t.bc.party(row.party)}
      <br />
      <b>{t.bc.cell[row.cells[j] as keyof T["bc"]["cell"]]}</b> — {col.subj}
      <br />
      <span className="muted">
        {longDate(col.date)} · {col.y}–{col.n} · {refs.map((p) => `${t.bc.party(p)}: ${word(grid.majority[j][p])}`).join(" · ")}
      </span>
    </div>
  );
}

// ---------- the selected vote ----------

const SWATCH = { y: "yes", n: "no", b: "abst", s: "skip", a: "away" } as const;

function VoteCard({ grid, view, cols }: { grid: Grid; view: View; cols: BarcodeCol[] }) {
  const { t, longDate } = useI18n();
  const c = (view.col != null && grid.colById.get(view.col)) || [...cols].sort((a, b) => closeness(b) - closeness(a))[0];
  if (!c) return null;
  const j = grid.index.get(c.id)!;
  const kinds = ["y", "n", "b", "s", "a"] as const;
  return (
    <section className="bc-card">
      <div>
        <p className="muted small">{view.col == null ? t.bc.pickColumn : t.bc.rollCall}</p>
        <h2>{c.subj}</h2>
        <p className="muted">{c.bill}</p>
        <p>
          <span className={`badge result-${c.res}`}>{t.result[c.res]}</span>{" "}
          <span className="mono small">
            {t.bc.counts(c.y, c.n, c.b)} · {t.bc.margin(Math.round(closeness(c) * 100))}
          </span>
        </p>
        <p className="bc-cite">
          {t.gazette(c.gac, null, c.page)} · {longDate(c.date)}
        </p>
        <a href={`#/vote/${c.id}`}>{t.bc.openVote} →</a>
      </div>
      <div className="bc-parties">
        <p className="muted small">{t.bc.partyBreakdown}</p>
        {grid.parties.map((p) => {
          const n = { y: 0, n: 0, b: 0, s: 0, a: 0 } as Record<(typeof kinds)[number], number>;
          for (const r of grid.rows) if (r.party === p && r.cells[j] in n) n[r.cells[j] as "y"]++;
          const total = kinds.reduce((s, k) => s + n[k], 0);
          if (!total) return null;
          return (
            <div key={p} className="bc-prow" style={{ ["--c" as string]: partyColor(p) }}>
              <span>
                <i /> {t.bc.party(p)}
              </span>
              <div className="bc-stack">
                {kinds.map((k) => (
                  <div key={k} className={`sw ${SWATCH[k]}`} style={{ width: `${(n[k] / total) * 100}%` }} />
                ))}
              </div>
              <span className="mono small">
                {n.y}–{n.n}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------- asking ----------

function Ask({ grid, view, onView }: { grid: Grid; view: View; onView: (v: View) => void }) {
  const { t, lang } = useI18n();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [replies, setReplies] = useState<(AskReply | null)[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const log = useRef<HTMLDivElement>(null);
  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" });
  }, [turns.length, busy]);

  const send = async (question: string) => {
    question = question.trim();
    if (!question || busy) return;
    const next: Turn[] = [...turns, { role: "user", content: question }];
    setTurns(next);
    setReplies([...replies, null]);
    setText("");
    setBusy(true);
    setError("");
    try {
      const reply = await api.ask({ turns: next.slice(-8), view, lang });
      setTurns([...next, { role: "assistant", content: JSON.stringify({ answer: reply.answer, view: reply.view }) }]);
      setReplies((r) => [...r.slice(0, -1), reply]);
      if (reply.view) onView(withDefaults(reply.view as Partial<View>));
    } catch (e) {
      setTurns(turns);
      setReplies(replies);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="bc-ask">
      <p className="muted small">{t.bc.askTitle}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(text);
        }}
      >
        <input value={text} maxLength={600} placeholder={t.bc.askPlaceholder} onChange={(e) => setText(e.target.value)} disabled={busy} aria-label={t.bc.askTitle} />
        <button type="submit" disabled={busy || !text.trim()}>
          {t.bc.askButton}
        </button>
      </form>
      {!turns.length && (
        <div className="chips">
          {t.bc.examples.map((x) => (
            <button key={x} className="chip dashed" onClick={() => send(x)}>
              {x}
            </button>
          ))}
        </div>
      )}
      {!!turns.length && (
        <div className="bc-log" ref={log}>
          {turns
            .map((turn, i) => [turn, i] as const)
            .filter(([turn]) => turn.role === "user")
            .map(([turn, i], n) => {
              const reply = replies[n];
              return (
                <div key={i} className="bc-exchange">
                  <p className="bc-q">{turn.content}</p>
                  {reply ? (
                    <div className="bc-a">
                      <Answer text={reply.answer} grid={grid} view={view} onView={onView} />
                      {!!reply.queries.length && <p className="muted small">{t.bc.lookedUp(reply.queries.map((q) => queryLabel(q, grid)).join(" · "))}</p>}
                    </div>
                  ) : (
                    busy && <p className="muted bc-thinking">{t.bc.thinking}</p>
                  )}
                </div>
              );
            })}
        </div>
      )}
      {error && <p className="status error">{t.bc.askError(error)}</p>}
      <p className="muted small">{t.bc.askNote}</p>
    </section>
  );
}

function queryLabel(q: Record<string, unknown>, grid: Grid): string {
  const bits = [];
  if (q.topic) bits.push(`“${q.topic}”`);
  if (q.member != null) bits.push(grid.rowById.get(Number(q.member))?.name ?? String(q.member));
  if (q.party) bits.push(String(q.party));
  if (q.metric) bits.push(String(q.metric).replace(/_/g, " "));
  if (q.min_share != null) bits.push(`≥${q.min_share}%`);
  return bits.join(", ") || "all votes";
}

// Answer text with **bold**, [v:ID] and [m:ID] turned into buttons.
function Answer({ text, grid, view, onView }: { text: string; grid: Grid; view: View; onView: (v: View) => void }) {
  const { longDate } = useI18n();
  const parts: ReactNode[] = [];
  const re = /\*\*(.+?)\*\*|\[v:(\d+)\]|\[m:(\d+)\]|\n/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    parts.push(text.slice(last, m.index));
    last = m.index + m[0].length;
    const key = m.index;
    if (m[1]) parts.push(<b key={key}>{m[1]}</b>);
    else if (m[2]) {
      const c = grid.colById.get(Number(m[2]));
      const visible = c && columns(grid, view).includes(c);
      parts.push(
        c ? (
          <button key={key} className="ref" title={c.subj} onClick={() => onView(visible ? { ...view, col: c.id } : { ...view, col: c.id, q: "", min: 0, types: [], from: "", to: "" })}>
            {longDate(c.date)} · {c.subj.length > 44 ? `${c.subj.slice(0, 44)}…` : c.subj}
          </button>
        ) : (
          m[0]
        ),
      );
    } else if (m[3]) {
      const r = grid.rowById.get(Number(m[3]));
      parts.push(
        r ? (
          <button key={key} className="ref member" style={{ ["--c" as string]: partyColor(r.party) }} onClick={() => onView({ ...view, pins: view.pins.includes(r.id) ? view.pins : [...view.pins, r.id] })}>
            {r.name}
          </button>
        ) : (
          m[0]
        ),
      );
    } else parts.push(<br key={key} />);
  }
  parts.push(text.slice(last));
  return <p>{parts}</p>;
}
