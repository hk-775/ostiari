export class MemoryStorage implements Storage {
  private values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return Array.from(this.values.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, String(value));
  }
}

export interface BrowserEnvironment {
  assignedLocations: string[];
  localStorage: MemoryStorage;
  sessionStorage: MemoryStorage;
}

export function installBrowserEnvironment(pathname = "/dashboard"): BrowserEnvironment {
  const localStorage = new MemoryStorage();
  const sessionStorage = new MemoryStorage();
  const assignedLocations: string[] = [];

  Object.defineProperties(globalThis, {
    localStorage: { value: localStorage, configurable: true },
    sessionStorage: { value: sessionStorage, configurable: true },
    window: {
      value: {
        location: {
          pathname,
          search: "",
          hash: "",
          assign: (location: string) => assignedLocations.push(location),
        },
      },
      configurable: true,
    },
  });

  return { assignedLocations, localStorage, sessionStorage };
}
