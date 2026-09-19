import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { asArray } from "@/lib/asArray";

export interface AiSubjects {
  subjects: { id: string; label: string }[];
  configured: boolean;
  provider: string;
  model: string;
}

export interface SubjectState {
  evidence: unknown;
  answer: string | null;
  asking: boolean;
  refusal: string | null;
}

const EMPTY: SubjectState = { evidence: null, answer: null, asking: false, refusal: null };

/**
 * All three analysis subjects at once, on one page.
 *
 * The React port put them behind a three-way selector, so reading the channel
 * report meant losing the generator's. The NiceGUI page was one scrolling
 * page — the owner asked for that back on 2026-09-19.
 *
 * **Evidence loads for all three; no model is called for any of them.** The
 * numbers are the answer most of the time and reading them is free, which is
 * why they are a separate endpoint. Each section has its own Ask button, so
 * one page does not mean three bills.
 */
export function useAiController() {
  const [meta, setMeta] = useState<AiSubjects | null>(null);
  const [days, setDays] = useState(30);
  const [bySubject, setBySubject] = useState<Record<string, SubjectState>>({});
  const [refusal, setRefusal] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api
      .get<AiSubjects>("/api/ai/subjects")
      .then((m) => !cancelled && setMeta({ ...m, subjects: asArray(m.subjects) }))
      .catch((e: Error) => !cancelled && setRefusal(e.message));
    return () => { cancelled = true; };
  }, []);

  const subjectIds = (meta?.subjects ?? []).map((s) => s.id).join(",");

  useEffect(() => {
    if (!subjectIds) return;
    let cancelled = false;
    // A fresh window is a different question, so the previous answers go with
    // it rather than sitting under numbers they were not about.
    setBySubject({});
    for (const id of subjectIds.split(",")) {
      void api
        .get<{ evidence: unknown }>(`/api/ai/evidence?subject=${id}&days=${days}`)
        .then((b) => !cancelled && setBySubject((prev) => ({
          ...prev, [id]: { ...(prev[id] ?? EMPTY), evidence: b.evidence },
        })))
        .catch((e: Error) => !cancelled && setBySubject((prev) => ({
          ...prev, [id]: { ...(prev[id] ?? EMPTY), refusal: e.message },
        })));
    }
    return () => { cancelled = true; };
  }, [subjectIds, days]);

  const analyse = useCallback(async (subject: string) => {
    setBySubject((prev) => ({
      ...prev,
      [subject]: { ...(prev[subject] ?? EMPTY), asking: true, refusal: null },
    }));
    try {
      const body = await api.post<{ answer: string }>("/api/ai/analyse", { subject, days });
      setBySubject((prev) => ({
        ...prev,
        [subject]: { ...(prev[subject] ?? EMPTY), asking: false, answer: body.answer },
      }));
    } catch (e) {
      setBySubject((prev) => ({
        ...prev,
        [subject]: {
          ...(prev[subject] ?? EMPTY), asking: false,
          refusal: e instanceof ApiError ? e.message : String(e),
        },
      }));
    }
  }, [days]);

  return {
    meta, days, setDays, refusal, analyse,
    stateFor: (subject: string): SubjectState => bySubject[subject] ?? EMPTY,
  };
}
