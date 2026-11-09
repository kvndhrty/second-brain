import { useCallback, useEffect, useRef, useState } from "react";

export interface Loaded<T> {
  data: T | undefined;
  error: string | undefined;
  loading: boolean;
  reload: () => void;
}

// Load data on mount and whenever `deps` change; `reload` fetches again.
export function useLoad<T>(load: () => Promise<T>, deps: unknown[] = []): Loaded<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadRef
      .current()
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(undefined);
        }
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

// Run `fn` every `ms` while `active` is true.
export function useInterval(fn: () => void, ms: number, active: boolean): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => fnRef.current(), ms);
    return () => clearInterval(id);
  }, [ms, active]);
}

export interface Action {
  busy: boolean;
  error: string | undefined;
  run: (fn: () => Promise<unknown>) => Promise<void>;
}

// Run async actions with a busy flag and an error message. With stayBusy, the
// flag stays set after success, for actions after which the component goes away
// (a reloaded list or a new page), so its buttons cannot be pressed twice.
export function useAction(stayBusy = false): Action {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const run = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setError(undefined);
      try {
        await fn();
        if (!stayBusy) setBusy(false);
      } catch (e) {
        setError((e as Error).message);
        setBusy(false);
      }
    },
    [stayBusy],
  );
  return { busy, error, run };
}
