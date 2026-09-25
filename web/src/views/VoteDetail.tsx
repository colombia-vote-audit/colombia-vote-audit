import { api, type Person, type Position, type VoteDetail as Vote } from "../api";
import { CheckBadge, ResultBadge, Status, useFetch } from "../common";
import { useI18n } from "../i18n";
import { href } from "../router";

const POSITIONS: Position[] = ["yes", "no", "abstain", "absent"];

export function VoteDetail({ id, back }: { id: number; back: string }) {
  const { t } = useI18n();
  const { data: v, loading, error } = useFetch(() => api.vote(id), [id]);
  return (
    <section className="detail">
      <a className="back" href={back}>
        ← {t.allVotes}
      </a>
      <Status loading={loading && !v} error={error}>
        {v && <Body v={v} />}
      </Status>
    </section>
  );
}

function Body({ v }: { v: Vote }) {
  const { t, longDate } = useI18n();
  const shown = POSITIONS.filter((p) => p !== "absent" || v.groups.absent);
  const count = (p: Position) => v.counts[p] ?? 0;
  const all = shown.reduce((n, p) => n + count(p), 0) || 1;
  const flagged = v.date_source === "gazette" || v.date_source === "publication";
  return (
    <>
      <div className="meta">
        {v.date && (
          <span className={`mono${flagged ? " flagged" : ""}`}>
            {longDate(v.date)}
            {flagged && <sup>*</sup>}
          </span>
        )}
        {v.chamber && <span>{v.chamber}</span>}
        {v.vote_type && <span className="caps">{t.voteType[v.vote_type]}</span>}
        <ResultBadge result={v.result} />
        <CheckBadge vote={v} />
      </div>
      {flagged && <p className="date-note">* {t.dateFlag[v.date_source as "gazette"]}</p>}
      <h1>{v.subject ?? v.bill_name}</h1>
      {v.bill_name && v.subject && <p className="bill">{v.bill_name}</p>}
      {v.bill_title && <p className="bill-title">{v.bill_title}</p>}
      {v.description && <p className="description">{v.description}</p>}

      <div className="tally">
        {shown.map((p) => (
          <span key={p} className={`c-${p}`}>
            <b>{count(p)}</b> {t.position[p]}
          </span>
        ))}
      </div>
      <div className="bar">
        {shown.map((p) => (
          <span key={p} className={`bg-${p}`} style={{ width: `${(100 * count(p)) / all}%` }} />
        ))}
      </div>

      <Source v={v} />

      <div className={`columns n${shown.length}`}>
        {shown.map((p) => (
          <Column key={p} position={p} people={v.groups[p] ?? []} />
        ))}
      </div>
      {!v.groups.absent && <p className="note">{t.absentUnknown}</p>}
    </>
  );
}

function Source({ v }: { v: Vote }) {
  const { t } = useI18n();
  const g = v.gazette;
  const label = t.gazette(g.number, g.published?.slice(0, 4) ?? null, g.page);
  const pdf = g.pdf && `${g.pdf}${g.page ? `#page=${g.page}` : ""}`;
  return (
    <div className={`source${v.verified === 0 ? " failed" : ""}`}>
      <p className="mono">
        {pdf ? (
          <a href={pdf} target="_blank" rel="noopener">
            {label} ↗
          </a>
        ) : (
          label
        )}
      </p>
      {v.verified === 1 && <p className="ok">{t.checked}</p>}
      {v.verified === 0 && <p className="warn">⚠ {t.checkFailed}</p>}
      {v.source === "text" && <p>{t.fromText}</p>}
      {v.check_note && <p className={v.verified === 0 ? "warn" : ""}>{v.check_note}</p>}
      {!g.pdf && <p className="muted">{t.noPdf}</p>}
    </div>
  );
}

function Column({ position, people }: { position: Position; people: Person[] }) {
  const { t } = useI18n();
  return (
    <div className={`column c-${position}`}>
      <h3>
        {t.position[position]} <span>{people.length}</span>
      </h3>
      {people.length ? (
        <ul>
          {people.map((p, i) => (
            <li key={i}>
              {p.id ? <a href={href(`/legislator/${p.id}`)}>{p.name}</a> : <span>{p.name}</span>}
              {p.party && <small>{p.party}</small>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">{t.noneRecorded}</p>
      )}
    </div>
  );
}
