// Helpers for the barcode page: the view it shows (kept in the URL so every
// view is a link) and figures derived from the grid the API sends.

import type { BarcodeCol, BarcodeData, BarcodeRow, VoteType } from "./api";

// How cells are colored:
//   ab       sided with party A or party B, where the two voted differently
//   with     same side as party A's majority or not
//   party    with the member's own party majority or not
//   vote     yes / no
//   outcome  on the winning side or not
//   attend   voted, in session without voting, absent
export type Color = "ab" | "with" | "party" | "vote" | "outcome" | "attend";
export type Order = "date" | "a" | "margin";
export type Sort = "party" | "align";
export type Rows = "compact" | "tall" | "named";

export const COLORS: Color[] = ["ab", "with", "party", "vote", "outcome", "attend"];
export const VOTE_TYPES: VoteType[] = ["final_passage", "articles", "report_motion", "procedural", "impedimento"];

export interface View {
  q: string;
  color: Color;
  a: string; // party A
  b: string; // party B
  order: Order;
  sort: Sort;
  min: number; // the losing side's least share of yes + no, in percent; 0 shows every vote
  types: VoteType[];
  from: string;
  to: string;
  pins: number[];
  parties: string[];
  col: number | null;
  rows: Rows;
}

export const DEFAULT_VIEW: View = {
  q: "",
  color: "ab",
  a: "Pacto Histórico",
  b: "Centro Democrático",
  order: "date",
  sort: "party",
  min: 15,
  types: [],
  from: "",
  to: "",
  pins: [],
  parties: [],
  col: null,
  rows: "compact",
};

export const ROW_HEIGHT: Record<Rows, number> = { compact: 4, tall: 8, named: 14 };

export const PARTY_COLORS: Record<string, string> = {
  "Pacto Histórico": "#b04aa0",
  Comunes: "#e0527a",
  "Alianza Verde": "#3fae54",
  Conservador: "#4a6fd1",
  Liberal: "#e0453a",
  "Centro Democrático": "#3ea6de",
  "Cambio Radical": "#ec7a1c",
  "La U": "#e3b505",
  "MIRA · Justa Libres": "#1aa3a8",
  "Other parties": "#8b93a0",
};

export function withDefaults(v: Partial<View> | undefined): View {
  return { ...DEFAULT_VIEW, ...(v ?? {}) };
}

// ----- the URL -----

export function viewFromParams(p: URLSearchParams): View {
  const pick = <T extends string>(key: string, allowed: readonly T[], fallback: T): T =>
    (allowed as readonly string[]).includes(p.get(key) ?? "") ? (p.get(key) as T) : fallback;
  const list = (key: string) => (p.get(key) ?? "").split(",").filter(Boolean);
  const min = Number(p.get("min") ?? DEFAULT_VIEW.min);
  return {
    q: p.get("q") ?? "",
    color: pick("color", COLORS, DEFAULT_VIEW.color),
    a: p.get("a") || DEFAULT_VIEW.a,
    b: p.get("b") || DEFAULT_VIEW.b,
    order: pick("order", ["date", "a", "margin"] as const, "date"),
    sort: pick("sort", ["party", "align"] as const, "party"),
    min: Number.isFinite(min) ? Math.max(0, Math.min(50, Math.round(min))) : DEFAULT_VIEW.min,
    types: list("types").filter((t): t is VoteType => (VOTE_TYPES as string[]).includes(t)),
    from: p.get("from") ?? "",
    to: p.get("to") ?? "",
    pins: list("pins").map(Number).filter(Number.isFinite),
    parties: list("parties"),
    col: p.get("col") ? Number(p.get("col")) : null,
    rows: pick("rows", ["compact", "tall", "named"] as const, "compact"),
  };
}

export function viewParams(v: View): Record<string, string | undefined> {
  const d = DEFAULT_VIEW;
  const unless = <K extends keyof View>(k: K) => (v[k] === d[k] ? undefined : String(v[k]));
  return {
    q: v.q || undefined,
    color: unless("color"),
    a: unless("a"),
    b: unless("b"),
    order: unless("order"),
    sort: unless("sort"),
    min: unless("min"),
    types: v.types.length ? v.types.join(",") : undefined,
    from: v.from || undefined,
    to: v.to || undefined,
    pins: v.pins.length ? v.pins.join(",") : undefined,
    parties: v.parties.length ? v.parties.join(",") : undefined,
    col: v.col != null ? String(v.col) : undefined,
    rows: unless("rows"),
  };
}

// ----- the grid -----

export function fold(s: string): string {
  return s
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

export interface Grid extends BarcodeData {
  colById: Map<number, BarcodeCol>;
  rowById: Map<number, BarcodeRow>;
  index: Map<number, number>; // column id -> position in cells
  search: string[];
  // Each party's majority (y / n / b) on each column, where a few members make one.
  majority: Record<string, string | null>[];
}

const MIN_PARTY = 4;

export function prepare(data: BarcodeData): Grid {
  const size: Record<string, number> = {};
  for (const r of data.rows) size[r.party] = (size[r.party] ?? 0) + 1;
  const majority = data.cols.map((_, j) => {
    const tally: Record<string, Record<string, number>> = {};
    for (const r of data.rows) {
      const v = r.cells[j];
      if ("ynb".includes(v) && size[r.party] >= MIN_PARTY) {
        tally[r.party] ??= { y: 0, n: 0, b: 0 };
        tally[r.party][v]++;
      }
    }
    const out: Record<string, string | null> = {};
    for (const [p, t] of Object.entries(tally)) {
      const [first, second] = Object.entries(t).sort((a, b) => b[1] - a[1]);
      out[p] = first[1] > second[1] ? first[0] : null;
    }
    return out;
  });
  return {
    ...data,
    colById: new Map(data.cols.map((c) => [c.id, c])),
    rowById: new Map(data.rows.map((r) => [r.id, r])),
    index: new Map(data.cols.map((c, j) => [c.id, j])),
    search: data.cols.map((c) => fold(`${c.subj} ${c.bill} ${c.title} ${c.type ?? ""}`)),
    majority,
  };
}

// Parties big enough to have a majority on a vote, for the A / B pickers.
export function referenceParties(g: Grid): string[] {
  return g.parties.filter((p) => g.majority.some((m) => m[p]));
}

// The losing side's share of the yes and no ballots, 0 to 0.5.
export function closeness(c: BarcodeCol): number {
  return c.y + c.n ? Math.min(c.y, c.n) / (c.y + c.n) : 0;
}

// Mirrors cva.barcode.contested: at least 20 yes/no ballots and the losing
// side at `min` percent or more. min 0 keeps every vote.
export function passes(c: BarcodeCol, min: number): boolean {
  return min <= 0 || (c.y + c.n >= 20 && closeness(c) * 100 >= min);
}

export function winner(c: BarcodeCol): string {
  return c.res === "approved" ? "y" : c.res === "rejected" ? "n" : c.y >= c.n ? "y" : "n";
}

// The columns a view shows, in its order.
export function columns(g: Grid, v: View): BarcodeCol[] {
  const q = fold(v.q);
  const pool = g.cols.filter((c) => passes(c, v.min) && (!v.types.length || (c.type && v.types.includes(c.type))));
  let found = q ? pool.filter((c) => g.search[g.index.get(c.id)!].includes(q)) : pool;
  if (q && !found.length) {
    const words = q.split(" ");
    found = pool.filter((c) => words.every((w) => g.search[g.index.get(c.id)!].includes(w)));
  }
  found = found.filter((c) => (!v.from || c.date >= v.from) && (!v.to || c.date <= v.to));
  if (v.order === "a") {
    const rank = (c: BarcodeCol) => ({ y: 0, n: 1 })[g.majority[g.index.get(c.id)!][v.a] ?? ""] ?? 2;
    found = [...found].sort((x, y) => rank(x) - rank(y));
  } else if (v.order === "margin") {
    found = [...found].sort((x, y) => closeness(y) - closeness(x));
  }
  return found;
}

// A member's share of ballots on party A's side over some columns, or null.
export function agreement(g: Grid, r: BarcodeRow, cols: BarcodeCol[], party: string): number | null {
  let same = 0;
  let n = 0;
  for (const c of cols) {
    const j = g.index.get(c.id)!;
    const v = r.cells[j];
    const m = g.majority[j][party];
    if (m && "ynb".includes(v)) {
      n++;
      same += +(v === m);
    }
  }
  return n ? same / n : null;
}
