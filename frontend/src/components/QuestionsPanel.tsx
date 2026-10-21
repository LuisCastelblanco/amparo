"use client";

import { useEffect, useRef } from "react";

export interface PendingQuestion {
  question: string;
  answer: string;
  skipped: boolean;
}

const focusRing = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-900";

/** Up to 3 optional questions asked before drafting, when the story misses something critical. */
export default function QuestionsPanel({
  questions,
  onChange,
  onContinue,
  disabled,
}: {
  questions: PendingQuestion[];
  onChange: (questions: PendingQuestion[]) => void;
  onContinue: () => void;
  disabled: boolean;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), []);

  const update = (i: number, patch: Partial<PendingQuestion>) =>
    onChange(questions.map((q, k) => (k === i ? { ...q, ...patch } : q)));

  return (
    <div className="mt-4 rounded-md border border-blue-200 bg-blue-50 p-3">
      <h3 ref={headingRef} tabIndex={-1} className="text-sm font-semibold text-blue-950 focus:outline-none">
        A few questions before drafting (optional)
      </h3>
      <p className="mt-0.5 text-xs text-blue-900">
        Your story seems to miss something important. Answer what you can, or skip.
      </p>
      <ol className="mt-3 space-y-3">
        {questions.map((q, i) => {
          const id = `question-${i}`;
          return (
            <li key={i}>
              <label htmlFor={id} lang="es" className="block text-sm text-slate-900">
                {q.question}
              </label>
              <div className="mt-1 flex gap-2">
                <input
                  id={id}
                  type="text"
                  lang="es"
                  value={q.skipped ? "" : q.answer}
                  disabled={q.skipped || disabled}
                  maxLength={500}
                  placeholder={q.skipped ? "Skipped" : "Your answer"}
                  onChange={(e) => update(i, { answer: e.target.value })}
                  className="min-w-0 flex-1 rounded-md border border-slate-400 bg-white px-2 py-1.5 text-sm placeholder:text-slate-500 disabled:bg-slate-100 focus:border-slate-700 focus:outline-none focus:ring-2 focus:ring-slate-300"
                />
                <button
                  type="button"
                  disabled={disabled}
                  aria-pressed={q.skipped}
                  aria-label={q.skipped ? `Answer question ${i + 1}` : `Skip question ${i + 1}`}
                  onClick={() => update(i, { skipped: !q.skipped, answer: "" })}
                  className={`shrink-0 rounded-md border border-slate-400 bg-white px-2.5 py-1.5 text-xs text-slate-800 hover:bg-slate-50 ${focusRing}`}
                >
                  {q.skipped ? "Answer" : "Skip"}
                </button>
              </div>
            </li>
          );
        })}
      </ol>
      <button
        type="button"
        onClick={onContinue}
        disabled={disabled}
        className={`mt-3 rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-60 ${focusRing}`}
      >
        Continue drafting
      </button>
    </div>
  );
}
