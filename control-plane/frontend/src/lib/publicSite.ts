export const IS_PUBLIC_SITE = import.meta.env.VITE_PUBLIC_SITE === "true";

export function publicAsset(path: string): string {
  return `${import.meta.env.BASE_URL}${path.replace(/^\/+/, "")}`;
}
