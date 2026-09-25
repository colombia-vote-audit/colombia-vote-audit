import { useState } from "react";
import { api, type LegislatorDetail as Legislator, type Position } from "../api";
import {
  Avatar,
  BackLink,
  PositionBadge,
  Status,
  VoteDate,
  shortChamber,
  termYears,
  useFetch,
  useTitle,
  type Back,
} from "../common";
import { useI18n } from "../i18n";
import { href } from "../router";
import { Comments } from "./Comments";

const PAGE = 100;

export function LegislatorDetail({ id, back }: { id: number; back: Back }) {
  const { data, loading, error } = useFetch(() => api.legislator(id), [id]);
  useTitle(data?.name);
  return (
    <section className="detail">
      <BackLink back={back} />
      <Status loading={loading && !data} error={error}>
        {data && <Body p={data} key={data.id} />}
      </Status>
    </section>
  );
}

function Body({ p }: { p: Legislator }) {
  const { t, num } = useI18n();
  const [shown, setShown] = useState(PAGE);
  const [only, setOnly] = useState<Position | null>(null);
  const record = only ? p.record.filter((r) => r.position === only) : p.record;
  const pick = (position: Position | null) => {
    setOnly(position);
    setShown(PAGE);
  };
  const cast = p.totals.yes + p.totals.no + p.totals.abstain;
  const span = p.terms.length ? termYears(p.terms[0].start, p.terms[p.terms.length - 1].end) : null;
  const stats: [string, number, Position | "cast"][] = [
    [t.votesCast, cast, "cast"],
    [t.position.yes, p.totals.yes, "yes"],
    [t.position.no, p.totals.no, "no"],
    [t.position.abstain, p.totals.abstain, "abstain"],
    [t.position.absent, p.totals.absent, "absent"],
  ];
  // The records have only Sí and No, so the tile is shown only when the text names some.
  // Who didn't vote is known only from checked records, which give a service window;
  // without one (every senator, for now) it's unknown, not zero.
  const tiles = stats.filter(
    ([, n, kind]) => (kind !== "abstain" || n > 0) && (kind !== "absent" || p.service.length > 0),
  );
  return (
    <>
      <header className="profile">
        <Avatar name={p.name} photo={p.photo_url} size={88} large />
        <div>
          <h1>{p.name}</h1>
          <p className="line">
            {p.party && <span>{p.party}</span>}
            {span && <span className="mono">{span}</span>}
          </p>
          {p.service.map((s) => (
            <p className="line" key={`${s.chamber}${s.term_start}`}>
              <span>{shortChamber(s.chamber)}</span>
              <span className="mono">{t.inOffice(s.first_vote, s.last_vote)}</span>
            </p>
          ))}
          {p.terms.map((term) => (
            <p className="line muted" key={`${term.chamber}${term.start}`}>
              <span>{shortChamber(term.chamber)}</span>
              <span className="mono">{termYears(term.start, term.end)}</span>
              {term.party && <span>{term.party}</span>}
            </p>
          ))}
        </div>
      </header>

      <div className="stats">
        {tiles.map(([label, n, kind]) => {
          const position = kind === "cast" ? null : kind;
          return (
            <button
              key={kind}
              className={`c-${kind}${only === position ? " on" : ""}`}
              aria-pressed={only === position}
              onClick={() => pick(position)}
            >
              <b>{num(n)}</b>
              <span>{label}</span>
              {kind === "absent" && <small>{t.inSessionCount(num(p.totals.absent_in_session))}</small>}
            </button>
          );
        })}
      </div>
      <p className="note">{t.totalsNote}</p>

      <h2 className="section">
        {t.votingRecord}
        {only && (
          <>
            {" · "}
            {t.position[only]} <button className="link" onClick={() => pick(null)}>{t.showAll}</button>
          </>
        )}
      </h2>
      <ul className="rows">
        {record.slice(0, shown).map((r) => (
          <li key={`${r.id}-${r.position}`}>
            <a className={`row${r.position === "absent" ? " dim" : ""}`} href={href(`/vote/${r.id}`)}>
              <VoteDate vote={r} />
              <span className="main">
                <strong>{r.subject ?? r.bill_name ?? `#${r.id}`}</strong>
                <small>
                  {[
                    r.chamber,
                    r.party_then && t.partyThen(r.party_then),
                    t.voteResult(t.result[r.result]),
                    r.in_session && t.inSessionThatDay,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </small>
              </span>
              <span className="side">
                <PositionBadge position={r.position} />
              </span>
            </a>
          </li>
        ))}
      </ul>
      {shown < record.length && (
        <button className="more" onClick={() => setShown(shown + PAGE)}>
          {t.showMore}
        </button>
      )}
      <Comments legislatorId={p.id} />
    </>
  );
}
