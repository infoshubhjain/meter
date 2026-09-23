"use client";

/**
 * Act 1 — the only screen a judge sees before their own Control Room.
 *
 * Name and email are the only required fields. Every credential is optional and
 * collapsed, because mandate approval alone is measured at 2-3 minutes and a credential
 * wall in front of someone who has not yet seen anything work is where they leave.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { JudgeError, judge, rememberToken } from "@/lib/judge";
import { Panel } from "@/components/ui/primitives";
import { TopNav } from "@/components/TopNav";

export function JudgeStart() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [open, setOpen] = useState(false);
  const [keys, setKeys] = useState({ openai: "", prava: "", linq: "", phone: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const session = await judge.createSession({
        name: name.trim(),
        email: email.trim(),
        openai_api_key: keys.openai.trim(),
        prava_api_key: keys.prava.trim(),
        poke_api_key: keys.linq.trim(),
        poke_phone: keys.phone.trim(),
      });
      rememberToken(session.token);
      // The cookie is what the server component reads, so the page has to be re-rendered
      // on the server rather than re-rendered on the client with the old props.
      router.refresh();
    } catch (err) {
      setError(err instanceof JudgeError ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <><TopNav /><main className="relative z-10 mx-auto w-full max-w-[760px] px-[32px] pb-[60px] pt-[48px]">
      <h1 className="t-display">Try it yourself</h1>
      <div className="t-eyebrow mb-[32px] mt-[8px]">
        Your own session · about 10 minutes · nothing to install
      </div>

      <Panel title="Start a session" tag="step 1 of 1">
        <form
          className="flex flex-col gap-[20px] p-[20px]"
          onSubmit={(event) => {
            event.preventDefault();
            void start();
          }}
        >
          <p className="t-body" style={{ color: "var(--color-text-secondary)" }}>
            You get your own Control Room — the same dashboard the team uses, showing only
            your calls. Every one is cost-predicted before it runs. Then you will watch a
            runaway feature get throttled, and an agent pay its own bill.
          </p>

          <div className="grid gap-[16px] sm:grid-cols-2">
            <Field label="Your name" required value={name} onChange={(v) => setName(v)}
                   placeholder="Ada Lovelace" />
            <Field label="Email" required type="email" value={email} onChange={(v) => setEmail(v)}
                   placeholder="ada@example.com"
                   hint="The identity on any payment mandate you approve." />
          </div>

          <button type="button" onClick={() => setOpen((v) => !v)}
                  className="text-left text-[13px] underline"
                  style={{ color: "var(--color-text-tertiary)" }}>
            {open ? "− Hide optional keys" : "+ Optional: bring your own keys"}
          </button>

          {open && (
            <div className="flex flex-col gap-[16px] rounded-[8px] p-[16px]"
                 style={{ background: "var(--color-surface-3)" }}>
              <p className="text-[12px]" style={{ color: "var(--color-text-tertiary)" }}>
                Every one of these is optional. Skip them all and the walkthrough still
                works end to end — it just runs on our keys instead of yours.
              </p>
              <Field label="OpenAI API key" secret value={keys.openai}
                     onChange={(v) => setKeys({ ...keys, openai: v })} placeholder="sk-…"
                     hint="Spends your credit instead of ours." />
              <Field label="Prava merchant key" secret value={keys.prava}
                     onChange={(v) => setKeys({ ...keys, prava: v })} placeholder="sk_test_…"
                     hint="With this, the charge settles on YOUR Prava account — your dashboard, your revoke button. Without it, the top-up stops before charging anything." />
              <Field label="Linq API key" secret value={keys.linq}
                     onChange={(v) => setKeys({ ...keys, linq: v })} placeholder="linq_…" />
              <Field label="Your phone" value={keys.phone}
                     onChange={(v) => setKeys({ ...keys, phone: v })}
                     placeholder="+15551234567"
                     hint="E.164 format. Linq's sandbox silently drops messages to a number that has not texted the sending line first — there is a test button once you start." />
            </div>
          )}

          {error && (
            <div role="alert" className="rounded-[8px] px-[16px] py-[12px] text-[13px]"
                 style={{ border: "1px solid var(--color-status-bad)",
                          color: "var(--color-status-bad)" }}>
              {error}
            </div>
          )}

          <button type="submit" className="judge-btn" disabled={busy}>
            {busy ? "Creating your session…" : "Start my session"}
          </button>

          <p className="text-[12px]" style={{ color: "var(--color-text-tertiary)" }}>
            Your dashboard shows only your own calls. They also appear in the public live
            feed under the name you enter — that feed is how anyone watching sees the
            product working. Your keys are different: held in memory for this session
            only, never written to the ledger, and dropped the moment you finish.
          </p>
        </form>
      </Panel>
    </main></>
  );
}

function Field({
  label, value, onChange, placeholder, hint, secret = false, required = false, type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  hint?: string;
  secret?: boolean;
  required?: boolean;
  type?: "email" | "text";
}) {
  return (
    <label className="flex flex-col gap-[6px] text-[13px]">
      <span style={{ color: "var(--color-text-secondary)" }}>{label}</span>
      <input className="judge-input" type={secret ? "password" : type} value={value}
             placeholder={placeholder} autoComplete="off"
             required={required}
             onChange={(e) => onChange(e.target.value)} />
      {hint && (
        <span className="text-[12px]" style={{ color: "var(--color-text-tertiary)" }}>
          {hint}
        </span>
      )}
    </label>
  );
}
