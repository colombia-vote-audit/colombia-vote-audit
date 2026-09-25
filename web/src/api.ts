// Types and fetchers for the API in src/cva/web.py.

export type Position = "yes" | "no" | "abstain" | "absent";
export type VoteType = "final_passage" | "articles" | "report_motion" | "impedimento" | "procedural";
// vote: the vote's own session date; gazette: shared by the other votes in its
// gazette; publication: the gazette's publication date.
export type DateSource = "vote" | "gazette" | "publication" | null;

export interface Counts {
  yes: number;
  no: number;
  abstain: number;
  absent: number | null; // null where absences can't be checked
  absent_in_session: number | null; // of those, how many voted on something else that day
}

export interface VoteSummary {
  id: number;
  document_id: number;
  date: string | null;
  date_source: DateSource;
  chamber: string | null;
  subject: string | null;
  bill_name: string | null;
  gaceta_number: string | null;
  vote_type: VoteType | null;
  result: "approved" | "rejected" | "unknown";
  source: "text" | "record";
  verified: 0 | 1 | null;
  counts: Counts;
}

export interface Person {
  id: number | null; // null for names read from the text that match no legislator
  name: string;
  photo_url: string | null;
  party: string | null;
  in_session?: boolean; // for members who didn't vote: whether they voted on something else that day
}

export interface VoteDetail extends VoteSummary {
  acta: string | null;
  bill_title: string | null;
  description: string | null;
  check_note: string | null;
  gazette: { number: string | null; published: string | null; page: number | null; pdf: string | null };
  groups: { yes: Person[]; no: Person[]; abstain: Person[]; absent: Person[] | null };
}

export interface Term {
  chamber: string;
  start: string;
  end: string;
  party: string | null;
}

export interface LegislatorSummary {
  id: number;
  name: string;
  photo_url: string | null;
  party: string | null;
  terms: Term[];
}

export interface LegislatorDetail extends LegislatorSummary {
  service: { chamber: string; term_start: string | null; first_vote: string; last_vote: string; votes: number }[];
  totals: Record<Position, number> & { absent_in_session: number };
  record: (VoteSummary & { position: Position; in_session: boolean | null; party_then: string | null })[];
}

// Comments on a legislator's page (cva.comments).
export interface LinkPreview {
  url: string;
  title: string | null;
  description: string | null;
  site_name: string | null;
  image: string | null;
}

export interface Comment {
  id: number;
  organization: string;
  author: string | null;
  body: string;
  created_at: string; // UTC, ISO 8601
  previews: LinkPreview[];
}

export interface Stats {
  votes: number;
  records: number;
  legislators: number;
  database_bytes: number;
  download_bytes: number; // gzipped
}

// The House barcode (cva.barcode). Cells: y yes, n no, b abstained,
// s in session but didn't vote, a absent that day, . not in the chamber.
export interface BarcodeRow {
  id: number;
  name: string;
  party: string; // party family, e.g. "Liberal"
  party_name: string | null;
  cells: string;
}

export interface BarcodeCol {
  id: number;
  date: string;
  res: "approved" | "rejected" | "unknown";
  type: VoteType | null;
  subj: string;
  bill: string;
  title: string;
  y: number;
  n: number;
  b: number;
  gov: "yes" | "no" | "abstain" | null; // how most Pacto Histórico members voted
  gac: string | null;
  page: number | null;
  doc: number;
  contested: boolean;
}

export interface BarcodeData {
  parties: string[];
  rows: BarcodeRow[];
  cols: BarcodeCol[];
  ask: boolean; // whether the server answers questions
}

export interface Turn {
  role: "user" | "assistant";
  content: string;
}

export interface AskReply {
  answer: string;
  view: Record<string, unknown> | null;
  queries: Record<string, unknown>[];
}

async function get<T>(path: string, params: Record<string, string | number | undefined> = {}): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") qs.set(k, String(v));
  const res = await fetch(qs.size ? `${path}?${qs}` : path);
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);
  return res.json();
}

export const api = {
  stats: () => get<Stats>("/api/stats"),
  votes: (p: { q?: string; from?: string; to?: string; chamber?: string; offset?: number; limit?: number }) =>
    get<{ total: number; votes: VoteSummary[] }>("/api/votes", p),
  vote: (id: number) => get<VoteDetail>(`/api/votes/${id}`),
  legislators: (q?: string) => get<{ total: number; legislators: LegislatorSummary[] }>("/api/legislators", { q }),
  legislator: (id: number) => get<LegislatorDetail>(`/api/legislators/${id}`),
  comments: (id: number) => get<{ comments: Comment[] }>(`/api/legislators/${id}/comments`),
  postComment: (id: number, body: { organization: string; author: string; body: string }) =>
    post<Comment>(`/api/legislators/${id}/comments`, body),
  barcode: () => get<BarcodeData>("/api/barcode"),
  ask: (body: { turns: Turn[]; view: unknown; lang: string }) => post<AskReply>("/api/ask", body),
};
