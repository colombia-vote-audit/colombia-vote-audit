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
  allLegislators: "All legislators",
  backToSearch: "Back to search",
  backToVote: "Back to the vote",
  backToLegislator: "Back to the legislator",
  bothChambers: "Both chambers",
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
  inSessionCount: (n: number | string) => `${n} in session`,
  absentInSession: "Voted on something else that day",
  absentAway: "No vote recorded that day",
  inSessionThatDay: "In session that day",
  noAbstain:
    "The voting records have only Yes and No: members can't abstain, only be excused over a conflict of interest (an impedimento).",
  absentUnknown:
    "Who didn't vote is known only for checked plenary votes read from scanned voting records.",
  inOffice: (a: string, b: string) => `In office per recorded votes ${a} → ${b}`,
  votesCast: "Votes cast",
  totalsNote:
    "Counts cover the votes in this database only. “Didn't vote” is known only for checked plenary votes read from scanned voting records; “in session” means they voted on something else that day. Pick a count to list only those votes.",
  votingRecord: "Voting record",
  showAll: "show all",
  partyThen: (p: string) => `Party then: ${p}`,
  voteResult: (r: string) => `Vote ${r.toLowerCase()}`,
  showMore: "Show more",
  notFound: "Not found.",
  downloadDb: "Download the database",
  downloadNote: (gz: string, db: string) =>
    `SQLite, gzipped: ${gz} MB (${db} MB unpacked). Every vote, name and check behind this site, plus the committee votes it leaves out.`,
  error: (m: string) => `Couldn't load: ${m}`,
  comments: {
    title: "Notes and sources",
    intro:
      "Data, reporting and links about this legislator, posted by organizations and groups. Posts are not checked by this site.",
    organization: "Organization or group",
    organizationPlaceholder: "e.g. Misión de Observación Electoral",
    author: "Your name (optional)",
    body: "Comment",
    bodyPlaceholder: "Share data or a link about this legislator…",
    post: "Post",
    posting: "Posting…",
    none: "No posts yet.",
    by: (a: string) => `by ${a}`,
    tooLong: (n: number) => `${n} characters over the limit`,
    failed: (m: string) => `Couldn't post: ${m}`,
    attach: "Attach files",
    attachNote: "or drop them here · up to 5 files, 25 MB each",
    remove: "Remove",
    tooMany: "Attach at most 5 files.",
    tooBig: (name: string) => `${name} is over 25 MB.`,
    tooBigTotal: "Attach at most 90 MB in all.",
  },
  bc: {
    nav: "Barcode",
    title: "The House barcode",
    intro:
      "Every ballot cast by every representative on the House's checked roll calls, 2023–2026, one cell each. Choose which votes to show and what the colors mean, drag across the grid to zoom in, and click a column to see that vote.",
    readMe:
      "Rows are representatives; columns are House plenary roll calls read from scanned voting records whose names add up to the printed totals. A party's position on a vote is how most of its members voted; parties with fewer than four members in the data have none. “In session, didn't vote” means the member voted on something else that day; “absent” means no vote was recorded that day. Party is the one each member was elected under for 2022–2026.",
    colorBy: "Color by",
    color: { ab: "Party A vs party B", with: "Agreement with party A", party: "Own party", vote: "Yes / no", outcome: "Winning side", attend: "Attendance" },
    partyA: "Party A",
    partyB: "Party B",
    key: (mode: string, a: string, b: string): [string, string][] => {
      const rest: [string, string][] = [["skip", "In session, didn't vote"], ["away", "Absent that day"]];
      switch (mode) {
        case "ab":
          return [["left", `Sided with ${a}`], ["right", `Sided with ${b}`], ["both", `${a} and ${b} agreed`], ["abst", "Abstained"], ...rest];
        case "with":
          return [["yes", `Same side as ${a}`], ["no", `Against ${a}`], ...rest];
        case "party":
          return [["yes", "With own party's majority"], ["no", "Broke with own party"], ["skip", "Didn't vote, or no party position"], ["away", "Absent that day"]];
        case "vote":
          return [["yes", "Yes"], ["no", "No"], ["abst", "Abstained"], ...rest];
        case "outcome":
          return [["yes", "On the winning side"], ["no", "On the losing side"], ...rest];
        default:
          return [["away", "Voted"], ["abst", "In session, didn't vote"], ["no", "Absent that day"]];
      }
    },
    notInChamber: "Not in the chamber",
    whichVotes: "Which votes",
    everyVote: "Every vote, however lopsided",
    threshold: (n: number) => `Only votes where the losing side got at least ${n}%`,
    shown: (n: string, total: string) => `${n} of ${total} roll calls`,
    voteTypes: "Kinds of vote:",
    topic: "Bill or topic",
    topicPlaceholder: "e.g. laboral, salud, 166 de 2023",
    topics: [
      ["Labor reform", "166 de 2023"],
      ["Pension reform", "pensional"],
      ["Health reform", "sistema de salud"],
    ] as [string, string][],
    zoomed: (a: string, b: string) => `Zoomed to ${a || "the start"} – ${b || "the end"}.`,
    resetZoom: "Reset zoom",
    arrange: "Arrange",
    columns: "Columns",
    byDate: "By date",
    byA: "By party A's vote",
    byMargin: "Closest first",
    rowsLabel: "Rows",
    byParty: "Grouped by party",
    byAgreement: "Ranked by agreement with party A",
    rows: { compact: "Compact", tall: "Tall", named: "Named" },
    pin: "Pin a representative",
    pinPlaceholder: "Type a name",
    unpin: "Unpin",
    showOnly: "Show only:",
    party: (p: string) => p,
    gridLabel: "Grid of every House member's ballot on every checked roll call",
    noMatch: "No roll calls match. Lower the threshold or clear the filters.",
    axis: (n: number) => `${n} roll calls · drag across the grid to zoom`,
    rollCalls: (n: number) => `${n} roll calls`,
    aYes: (p: string) => `${p} voted yes →`,
    aNo: (p: string) => `← ${p} voted no`,
    closest: "Closest votes →",
    widest: "← Most one-sided",
    cell: { y: "Yes", n: "No", b: "Abstained", s: "In session, didn't vote", a: "Absent", ".": "Not in the chamber" },
    split: "no position",
    pickColumn: "The closest vote shown · click any column to change",
    rollCall: "Roll call",
    counts: (y: number, n: number, b: number) => `${y} yes · ${n} no${b ? ` · ${b} abstained` : ""}`,
    margin: (pct: number) => `losing side ${pct}%`,
    openVote: "Open the vote and its gazette page",
    partyBreakdown: "How each party voted (yes · no · abstained · didn't vote · absent)",
    askTitle: "Ask the barcode",
    askPlaceholder: "e.g. Show the labor reform votes where the losing side got at least 40%",
    askButton: "Ask",
    examples: [
      "Show only votes where the losing side got at least 40%, colored La U vs Centro Democrático",
      "Rank every representative by agreement with the Pacto on the pension reform",
      "Who broke with their own party most on the labor reform?",
      "Who was in session but didn't vote on the closest votes?",
    ],
    thinking: "Looking through the votes…",
    lookedUp: (s: string) => `Looked up: ${s}`,
    askError: (m: string) => `No answer: ${m}`,
    askNote:
      "Answers are written by a language model from these records only; every figure comes from the grid. Check key numbers against the grid and the cited gazette pages.",
  },
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
  allLegislators: "Todos los congresistas",
  backToSearch: "Volver a la búsqueda",
  backToVote: "Volver a la votación",
  backToLegislator: "Volver al congresista",
  bothChambers: "Ambas cámaras",
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
  inSessionCount: (n) => `${n} en sesión`,
  absentInSession: "Votó en otras votaciones ese día",
  absentAway: "Sin votos registrados ese día",
  inSessionThatDay: "En sesión ese día",
  noAbstain:
    "Los registros de votación solo tienen Sí y No: los congresistas no pueden abstenerse, solo ser excusados por conflicto de interés (un impedimento).",
  absentUnknown:
    "Quién no votó solo se sabe para votaciones de plenaria verificadas, leídas de registros de votación escaneados.",
  inOffice: (a, b) => `En el cargo según votos registrados ${a} → ${b}`,
  votesCast: "Votos emitidos",
  totalsNote:
    "Los conteos cubren solo las votaciones de esta base de datos. “No votó” solo se sabe para votaciones de plenaria verificadas, leídas de registros de votación escaneados; “en sesión” significa que votó en otras votaciones ese día. Elija un conteo para ver solo esas votaciones.",
  votingRecord: "Historial de votaciones",
  showAll: "mostrar todo",
  partyThen: (p) => `Partido entonces: ${p}`,
  voteResult: (r) => `Votación: ${r.toLowerCase()}`,
  showMore: "Mostrar más",
  notFound: "No encontrado.",
  downloadDb: "Descargar la base de datos",
  downloadNote: (gz, db) =>
    `SQLite comprimido con gzip: ${gz} MB (${db} MB descomprimido). Todas las votaciones, nombres y verificaciones de este sitio, más las votaciones de comisión que no muestra.`,
  error: (m) => `No se pudo cargar: ${m}`,
  comments: {
    title: "Notas y fuentes",
    intro:
      "Datos, reportajes y enlaces sobre este congresista, publicados por organizaciones y grupos. Este sitio no verifica las publicaciones.",
    organization: "Organización o grupo",
    organizationPlaceholder: "p. ej. Misión de Observación Electoral",
    author: "Su nombre (opcional)",
    body: "Comentario",
    bodyPlaceholder: "Comparta datos o un enlace sobre este congresista…",
    post: "Publicar",
    posting: "Publicando…",
    none: "Aún no hay publicaciones.",
    by: (a) => `por ${a}`,
    tooLong: (n) => `${n} caracteres por encima del límite`,
    failed: (m) => `No se pudo publicar: ${m}`,
    attach: "Adjuntar archivos",
    attachNote: "o arrástrelos aquí · hasta 5 archivos de 25 MB cada uno",
    remove: "Quitar",
    tooMany: "Adjunte como máximo 5 archivos.",
    tooBig: (name) => `${name} supera los 25 MB.`,
    tooBigTotal: "Adjunte como máximo 90 MB en total.",
  },
  bc: {
    nav: "Código de barras",
    title: "El código de barras de la Cámara",
    intro:
      "Cada voto de cada representante en las votaciones nominales verificadas de la Cámara, 2023–2026, una celda por voto. Elija qué votaciones mostrar y qué significan los colores, arrastre sobre la cuadrícula para acercarse y haga clic en una columna para ver esa votación.",
    readMe:
      "Las filas son representantes; las columnas, votaciones nominales de la plenaria de la Cámara leídas de registros escaneados cuyos nombres suman los totales impresos. La posición de un partido en una votación es cómo votó la mayoría de sus miembros; los partidos con menos de cuatro miembros en los datos no tienen. “En sesión, no votó” significa que votó en otra votación ese día; “ausente”, que no hay votos registrados ese día. El partido es aquel por el que fue elegido para 2022–2026.",
    colorBy: "Colorear por",
    color: { ab: "Partido A vs partido B", with: "Acuerdo con el partido A", party: "Su partido", vote: "Sí / no", outcome: "Lado ganador", attend: "Asistencia" },
    partyA: "Partido A",
    partyB: "Partido B",
    key: (mode, a, b) => {
      const rest: [string, string][] = [["skip", "En sesión, no votó"], ["away", "Ausente ese día"]];
      switch (mode) {
        case "ab":
          return [["left", `Con ${a}`], ["right", `Con ${b}`], ["both", `${a} y ${b} coincidieron`], ["abst", "Se abstuvo"], ...rest];
        case "with":
          return [["yes", `Del lado de ${a}`], ["no", `En contra de ${a}`], ...rest];
        case "party":
          return [["yes", "Con la mayoría de su partido"], ["no", "Se apartó de su partido"], ["skip", "No votó, o su partido sin posición"], ["away", "Ausente ese día"]];
        case "vote":
          return [["yes", "Sí"], ["no", "No"], ["abst", "Se abstuvo"], ...rest];
        case "outcome":
          return [["yes", "Del lado ganador"], ["no", "Del lado perdedor"], ...rest];
        default:
          return [["away", "Votó"], ["abst", "En sesión, no votó"], ["no", "Ausente ese día"]];
      }
    },
    notInChamber: "No estaba en la Cámara",
    whichVotes: "Qué votaciones",
    everyVote: "Todas las votaciones, aun las muy desiguales",
    threshold: (n) => `Solo votaciones en que el lado perdedor obtuvo al menos ${n}%`,
    shown: (n, total) => `${n} de ${total} votaciones`,
    voteTypes: "Tipos de votación:",
    topic: "Proyecto o tema",
    topicPlaceholder: "p. ej. laboral, salud, 166 de 2023",
    topics: [
      ["Reforma laboral", "166 de 2023"],
      ["Reforma pensional", "pensional"],
      ["Reforma a la salud", "sistema de salud"],
    ],
    zoomed: (a, b) => `Acercado a ${a || "el inicio"} – ${b || "el final"}.`,
    resetZoom: "Quitar acercamiento",
    arrange: "Organizar",
    columns: "Columnas",
    byDate: "Por fecha",
    byA: "Por voto del partido A",
    byMargin: "Más reñidas primero",
    rowsLabel: "Filas",
    byParty: "Agrupadas por partido",
    byAgreement: "Por acuerdo con el partido A",
    rows: { compact: "Compacto", tall: "Alto", named: "Con nombres" },
    pin: "Fijar un representante",
    pinPlaceholder: "Escriba un nombre",
    unpin: "Quitar",
    showOnly: "Mostrar solo:",
    party: (p) => (p === "Other parties" ? "Otros partidos" : p),
    gridLabel: "Cuadrícula con el voto de cada representante en cada votación verificada",
    noMatch: "Ninguna votación coincide. Baje el umbral o quite filtros.",
    axis: (n) => `${n} votaciones · arrastre sobre la cuadrícula para acercarse`,
    rollCalls: (n) => `${n} votaciones`,
    aYes: (p) => `${p} votó sí →`,
    aNo: (p) => `← ${p} votó no`,
    closest: "Más reñidas →",
    widest: "← Más desiguales",
    cell: { y: "Sí", n: "No", b: "Se abstuvo", s: "En sesión, no votó", a: "Ausente", ".": "No estaba en la Cámara" },
    split: "sin posición",
    pickColumn: "La votación más reñida que se muestra · haga clic en una columna para cambiarla",
    rollCall: "Votación",
    counts: (y, n, b) => `${y} sí · ${n} no${b ? ` · ${b} abstenciones` : ""}`,
    margin: (pct) => `lado perdedor ${pct}%`,
    openVote: "Abrir la votación y su página de la gaceta",
    partyBreakdown: "Cómo votó cada partido (sí · no · abstención · no votó · ausente)",
    askTitle: "Pregúntele al código de barras",
    askPlaceholder: "p. ej. Muestra las votaciones de la reforma laboral en que el perdedor obtuvo al menos 40%",
    askButton: "Preguntar",
    examples: [
      "Muestra solo votaciones en que el perdedor obtuvo al menos 40%, coloreadas La U vs Centro Democrático",
      "Ordena a todos los representantes por acuerdo con el Pacto en la reforma pensional",
      "¿Quién se apartó más de su partido en la reforma laboral?",
      "¿Quién estaba en sesión pero no votó en las votaciones más reñidas?",
    ],
    thinking: "Revisando las votaciones…",
    lookedUp: (s) => `Consultó: ${s}`,
    askError: (m) => `Sin respuesta: ${m}`,
    askNote:
      "Las respuestas las escribe un modelo de lenguaje solo a partir de estos registros; cada cifra sale de la cuadrícula. Verifique las cifras clave en la cuadrícula y en las páginas de la gaceta citadas.",
  },
};

const dicts: Record<Lang, Dict> = { en, es };

interface I18n {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: Dict;
  num: (n: number) => string;
  longDate: (iso: string) => string;
  dateTime: (iso: string) => string;
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
    dateTime: (iso) =>
      new Date(iso).toLocaleString(locale, {
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }),
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18n {
  const v = useContext(Ctx);
  if (!v) throw new Error("useI18n outside I18nProvider");
  return v;
}
