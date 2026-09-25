import { Fragment } from "react";
import { api, type Person, type Position, type VoteDetail as Vote } from "../api";
import { BackLink, CheckBadge, ResultBadge, Status, useFetch, useTitle, type Back } from "../common";
import { useI18n } from "../i18n";
import { href } from "../router";

const POSITIONS: Position[] = ["yes", "no", "abstain", "absent"];

export function VoteDetail({ id, back }: { id: number; back: Back }) {
  const { data: v, loading, error } = useFetch(() => api.vote(id), [id]);
  useTitle(v && (v.subject ?? v.bill_name));
  return (
    <section className="detail">
      <BackLink back={back} />
      <Status loading={loading && !v} error={error}>
        {v && <Body v={v} />}
      </Status>
    </section>
  );
}

function Body({ v }: { v: Vote }) {
  const { t, longDate } = useI18n();
  // The records have only Sí and No, so abstentions are shown only when the text names some.
  const shown = POSITIONS.filter((p) => (p !== "absent" || v.groups.absent) && (p !== "abstain" || v.counts.abstain));
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
            {p === "absent" && <small> ({t.inSessionCount(v.counts.absent_in_session ?? 0)})</small>}
          </span>
        ))}
      </div>
      <div className="bar">
        {shown.map((p) =>
          p === "absent" ? (
            <Fragment key={p}>
              <span className="bg-absent-in" style={{ width: `${(100 * (v.counts.absent_in_session ?? 0)) / all}%` }} />
              <span className="bg-absent" style={{ width: `${(100 * (count(p) - (v.counts.absent_in_session ?? 0))) / all}%` }} />
            </Fragment>
          ) : (
            <span key={p} className={`bg-${p}`} style={{ width: `${(100 * count(p)) / all}%` }} />
          ),
        )}
      </div>

      <Source v={v} />

      <div className={`columns n${shown.length}`}>
        {shown.map((p) => (
          p === "absent" ? (
            <AbsentColumn key={p} people={v.groups.absent ?? []} />
          ) : (
            <Column key={p} position={p} people={v.groups[p] ?? []} />
          )
        ))}
      </div>
      <p className="note">
        {!v.counts.abstain && t.noAbstain} {!v.groups.absent && t.absentUnknown}
      </p>
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
      {people.length ? <People people={people} /> : <p className="muted">{t.noneRecorded}</p>}
    </div>
  );
}

// Members who didn't vote, split by whether they voted on something else that day.
function AbsentColumn({ people }: { people: Person[] }) {
  const { t } = useI18n();
  const groups: [string, Person[]][] = [
    [t.absentInSession, people.filter((p) => p.in_session)],
    [t.absentAway, people.filter((p) => !p.in_session)],
  ];
  return (
    <div className="column c-absent">
      <h3>
        {t.position.absent} <span>{people.length}</span>
      </h3>
      {!people.length && <p className="muted">{t.noneRecorded}</p>}
      {groups.map(
        ([label, group]) =>
          group.length > 0 && (
            <Fragment key={label}>
              <h4>
                {label} <span>{group.length}</span>
              </h4>
              <People people={group} />
            </Fragment>
          ),
      )}
    </div>
  );
}

function People({ people }: { people: Person[] }) {
  return (
    <ul>
      {people.map((p, i) => (
        <li key={i}>
          {p.id ? <a href={href(`/legislator/${p.id}`)}>{p.name}</a> : <span>{p.name}</span>}
          {p.party && <small>{p.party}</small>}
        </li>
      ))}
    </ul>
  );
}
