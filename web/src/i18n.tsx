import { createContext, useContext, useState, type ReactNode } from "react";
import type { DateSource, Position, VoteSummary, VoteType } from "./api";

export type Lang = "en" | "es";

const en = {
  title: "Colombia Vote Audit",
  tagline: "roll-call record",
  stats: (v: string, r: string, l: string) => `${v} votes · ${r} individual votes · ${l} legislators`,
  searchVotes: "Search votes by bill, topic or description",
  searchLegislators: "Search legislators by name",
  votes: "Votes",
  legislators: "Legislators",
  from: "From",
  to: "to",
  clearDates: "Clear dates",
  votesFound: (n: string) => `${n} votes, newest first`,
  legislatorsFound: (n: string) => `${n} legislators`,
  loadMore: "Load more",
  loading: "Loading…",
  noVotes: "No votes match.",
  allVotes: "All votes",
  backToSearch: "Back to search",
  result: { approved: "Approved", rejected: "Rejected", unknown: "Result unknown" },
  voteType: {
    final_passage: "Final passage",
    articles: "Articles",
    report_motion: "Report motion",
    impedimento: "Recusal",
    procedural: "Procedural",
  } satisfies Record<VoteType, string>,
  position: { yes: "Yes", no: "No", abstain: "Abstained", absent: "Didn't vote" } satisfies Record<Position, string>,
  voted: { yes: "Voted yes", no: "Voted no", abstain: "Abstained", absent: "Didn't vote" } satisfies Record<
    Position,
    string
  >,
  countsLine: (c: VoteSummary["counts"]) =>
    [
      `${c.yes} yes`,
      `${c.no} no`,
      c.abstain ? `${c.abstain} abstained` : null,
      c.absent !== null ? `${c.absent} didn't vote` : null,
    ]
      .filter(Boolean)
      .join(" · "),
  dateFlag: {
    gazette: "The record gives no date for this vote; this is the date of the other votes in the same gazette.",
    publication: "The record gives no date for this vote; this is the gazette's publication date.",
  } satisfies Record<Exclude<DateSource, "vote" | null>, string>,
  dateFlagShort: { gazette: "date from gazette", publication: "publication date" },
  gazette: (n: string | null, year: string | null, page: number | null) =>
    `Gaceta del Congreso ${n ?? "?"}${year ? ` de ${year}` : ""}${page ? `, p. ${page}` : ""}`,
  openPdf: "Open the gazette page",
  noPdf: "The gazette PDF isn't available on this server.",
  checked: "Checked: the names add up to the record's printed totals and the announced result.",
  checkFailed: "Check failed: these names may be incomplete or wrong.",
  checkFailedBadge: "Check failed",
  fromText: "Names read from the gazette text. They weren't checked against a voting record.",
  noneRecorded: "None recorded",
  absentUnknown:
    "Who didn't vote is known only for checked plenary votes read from scanned voting records.",
  inOffice: (a: string, b: string) => `In office per recorded votes ${a} → ${b}`,
  votesCast: "Votes cast",
  totalsNote:
    "Counts cover the votes in this database only. “Didn't vote” is known only for checked plenary votes read from scanned voting records.",
  votingRecord: "Voting record",
  partyThen: (p: string) => `Party then: ${p}`,
  voteResult: (r: string) => `Vote ${r.toLowerCase()}`,
  showMore: "Show more",
  notFound: "Not found.",
  error: (m: string) => `Couldn't load: ${m}`,
};

type Dict = typeof en;

const es: Dict = {
  title: "Auditoría de Votos de Colombia",
  tagline: "registro de votaciones nominales",
  stats: (v, r, l) => `${v} votaciones · ${r} votos individuales · ${l} congresistas`,
  searchVotes: "Buscar votaciones por proyecto, tema o descripción",
  searchLegislators: "Buscar congresistas por nombre",
  votes: "Votaciones",
  legislators: "Congresistas",
  from: "Desde",
  to: "hasta",
  clearDates: "Borrar fechas",
  votesFound: (n) => `${n} votaciones, las más recientes primero`,
  legislatorsFound: (n) => `${n} congresistas`,
  loadMore: "Cargar más",
  loading: "Cargando…",
  noVotes: "Ninguna votación coincide.",
  allVotes: "Todas las votaciones",
  backToSearch: "Volver a la búsqueda",
  result: { approved: "Aprobado", rejected: "Negado", unknown: "Resultado desconocido" },
  voteType: {
    final_passage: "Votación final",
    articles: "Articulado",
    report_motion: "Informe de ponencia",
    impedimento: "Impedimento",
    procedural: "Trámite",
  },
  position: { yes: "Sí", no: "No", abstain: "Abstención", absent: "No votó" },
  voted: { yes: "Votó sí", no: "Votó no", abstain: "Se abstuvo", absent: "No votó" },
  countsLine: (c) =>
    [
      `${c.yes} sí`,
      `${c.no} no`,
      c.abstain ? `${c.abstain} abstenciones` : null,
      c.absent !== null ? `${c.absent} no votaron` : null,
    ]
      .filter(Boolean)
      .join(" · "),
  dateFlag: {
    gazette: "El registro no da fecha para esta votación; es la fecha de las demás votaciones de la misma gaceta.",
    publication: "El registro no da fecha para esta votación; es la fecha de publicación de la gaceta.",
  },
  dateFlagShort: { gazette: "fecha de la gaceta", publication: "fecha de publicación" },
  gazette: en.gazette,
  openPdf: "Abrir la página de la gaceta",
  noPdf: "El PDF de la gaceta no está disponible en este servidor.",
  checked: "Verificado: los nombres suman los totales impresos del registro y el resultado anunciado.",
  checkFailed: "Verificación fallida: estos nombres pueden estar incompletos o errados.",
  checkFailedBadge: "No verificado",
  fromText: "Nombres leídos del texto de la gaceta. No se verificaron contra un registro de votación.",
  noneRecorded: "Ninguno registrado",
  absentUnknown:
    "Quién no votó solo se sabe para votaciones de plenaria verificadas, leídas de registros de votación escaneados.",
  inOffice: (a, b) => `En el cargo según votos registrados ${a} → ${b}`,
  votesCast: "Votos emitidos",
  totalsNote:
    "Los conteos cubren solo las votaciones de esta base de datos. “No votó” solo se sabe para votaciones de plenaria verificadas, leídas de registros de votación escaneados.",
  votingRecord: "Historial de votaciones",
  partyThen: (p) => `Partido entonces: ${p}`,
  voteResult: (r) => `Votación: ${r.toLowerCase()}`,
  showMore: "Mostrar más",
  notFound: "No encontrado.",
  error: (m) => `No se pudo cargar: ${m}`,
};

const dicts: Record<Lang, Dict> = { en, es };

interface I18n {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: Dict;
  num: (n: number) => string;
  longDate: (iso: string) => string;
}

const Ctx = createContext<I18n | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => {
    const saved = localStorage.getItem("lang");
    if (saved === "en" || saved === "es") return saved;
    return navigator.language.startsWith("es") ? "es" : "en";
  });
  const setLang = (l: Lang) => {
    localStorage.setItem("lang", l);
    document.documentElement.lang = l;
    setLangState(l);
  };
  const locale = lang === "es" ? "es-CO" : "en-US";
  const value: I18n = {
    lang,
    setLang,
    t: dicts[lang],
    num: (n) => n.toLocaleString(locale),
    longDate: (iso) =>
      new Date(`${iso}T12:00:00`).toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric" }),
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18n {
  const v = useContext(Ctx);
  if (!v) throw new Error("useI18n outside I18nProvider");
  return v;
}
