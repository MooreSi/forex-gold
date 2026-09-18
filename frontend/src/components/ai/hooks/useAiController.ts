import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { asArray } from "@/lib/asArray";

export interface AiSubjects {
  subjects: { id: string; label: string }[];
  configured: boolean;
  provider: string;
  model: string;
}

export function useAiController() {
  const [meta, setMeta] = useState<AiSubjects | null>(null);
  const [subject, setSubject] = useState("channels");
  const [days, setDays] = useState(30);
  const [evidence, setEvidence] = useState<unknown>(null);
  const [answer, setAnswer] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api
      .get<AiSubjects>("/api/ai/subjects")
      .then((m) => !cancelled && setMeta({ ...m, subjects: asArray(m.subjects) }))
      .catch((e: Error) => !cancelled && setRefusal(e.message));
    return () => {
      cancelled = true;
    };
  }, []);

  // Evidence is free, so it loads on its own. The model is not, so it does not.
  useEffect(() => {
    let cancelled = false;
    setEvidence(null);
    setAnswer(null);
    void api
      .get<{ evidence: unknown }>(`/api/ai/evidence?subject=${subject}&days=${days}`)
      .then((b) => !cancelled && setEvidence(b.evidence))
      .catch((e: Error) => !cancelled && setRefusal(e.message));
    return () => {
      cancelled = true;
    };
  }, [subject, days]);

  const analyse = useCallback(async () => {
    setAsking(true);
    setRefusal(null);
    try {
      const body = await api.post<{ answer: string }>("/api/ai/analyse", { subject, days });
      setAnswer(body.answer);
    } catch (e) {
      setRefusal(e instanceof ApiError ? e.message : String(e));
    } finally {
      setAsking(false);
    }
  }, [subject, days]);

  return {
    meta, subject, setSubject, days, setDays,
    evidence, answer, asking, refusal, analyse,
  };
}
