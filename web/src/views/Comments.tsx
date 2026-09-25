import { useState, type FormEvent, type ReactNode } from "react";
import { api, type Comment, type LinkPreview } from "../api";
import { Status, useFetch } from "../common";
import { useI18n } from "../i18n";

const MAX_BODY = 4000; // cva.comments.MAX_BODY
const LINK = /https?:\/\/[^\s<>"']+/g;

const count = (s: string, c: string) => s.split(c).length - 1;

// Trimmed as cva.comments.links does, so links line up with their previews.
function trimLink(url: string): string {
  let u = url.replace(/[.,;:!?]+$/, "");
  while (u.endsWith(")") && count(u, ")") > count(u, "(")) u = u.slice(0, -1).replace(/[.,;:!?]+$/, "");
  return u;
}

function Linked({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(LINK)) {
    const url = trimLink(m[0]);
    parts.push(text.slice(last, m.index));
    parts.push(
      <a key={m.index} href={url} target="_blank" rel="nofollow ugc noopener noreferrer">
        {url}
      </a>,
    );
    last = m.index + url.length;
  }
  parts.push(text.slice(last));
  return <p className="comment-body">{parts}</p>;
}

function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function Preview({ p }: { p: LinkPreview }) {
  const [broken, setBroken] = useState(false);
  return (
    <a className="preview" href={p.url} target="_blank" rel="nofollow ugc noopener noreferrer">
      {p.image && !broken && (
        <img src={p.image} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setBroken(true)} />
      )}
      <span>
        <small>{p.site_name ?? host(p.url)}</small>
        {p.title && <strong>{p.title}</strong>}
        {p.description && <span className="muted">{p.description}</span>}
      </span>
    </a>
  );
}

function Post({ c }: { c: Comment }) {
  const { t, dateTime } = useI18n();
  return (
    <li className="comment">
      <p className="comment-head">
        <strong>{c.organization}</strong>
        {c.author && <span className="muted">{t.comments.by(c.author)}</span>}
        <time className="mono muted" dateTime={c.created_at}>
          {dateTime(c.created_at)}
        </time>
      </p>
      <Linked text={c.body} />
      {c.previews.map((p) => (
        <Preview key={p.url} p={p} />
      ))}
    </li>
  );
}

function Form({ legislatorId, onPosted }: { legislatorId: number; onPosted: () => void }) {
  const { t } = useI18n();
  const [organization, setOrganization] = useState(() => localStorage.getItem("commentOrg") ?? "");
  const [author, setAuthor] = useState(() => localStorage.getItem("commentAuthor") ?? "");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const over = body.trim().length - MAX_BODY;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.postComment(legislatorId, { organization, author, body });
      localStorage.setItem("commentOrg", organization.trim());
      localStorage.setItem("commentAuthor", author.trim());
      setBody("");
      onPosted();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="comment-form" onSubmit={submit}>
      <div className="comment-names">
        <label>
          {t.comments.organization}
          <input
            required
            maxLength={120}
            value={organization}
            placeholder={t.comments.organizationPlaceholder}
            onChange={(e) => setOrganization(e.target.value)}
          />
        </label>
        <label>
          {t.comments.author}
          <input maxLength={120} value={author} onChange={(e) => setAuthor(e.target.value)} />
        </label>
      </div>
      <label>
        {t.comments.body}
        <textarea
          required
          rows={4}
          value={body}
          placeholder={t.comments.bodyPlaceholder}
          onChange={(e) => setBody(e.target.value)}
        />
      </label>
      <div className="comment-actions">
        {over > 0 && <span className="error">{t.comments.tooLong(over)}</span>}
        {error && <span className="error">{t.comments.failed(error)}</span>}
        <button type="submit" disabled={busy || over > 0 || !organization.trim() || !body.trim()}>
          {busy ? t.comments.posting : t.comments.post}
        </button>
      </div>
    </form>
  );
}

export function Comments({ legislatorId }: { legislatorId: number }) {
  const { t } = useI18n();
  const [version, setVersion] = useState(0);
  const { data, loading, error } = useFetch(() => api.comments(legislatorId), [legislatorId, version]);
  return (
    <section className="comments">
      <h2 className="section">{t.comments.title}</h2>
      <p className="note">{t.comments.intro}</p>
      <Form legislatorId={legislatorId} onPosted={() => setVersion(version + 1)} />
      <Status loading={loading && !data} error={error}>
        {data && data.comments.length === 0 && <p className="status">{t.comments.none}</p>}
        <ul className="comment-list">
          {data?.comments.map((c) => (
            <Post key={c.id} c={c} />
          ))}
        </ul>
      </Status>
    </section>
  );
}
