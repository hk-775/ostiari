export const SSO_RETURN_KEY = "ostiari_sso_return_to";

export function safeReturnPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return "/dashboard";
  }
  if (value === "/login" || value.startsWith("/auth/sso-callback")) {
    return "/dashboard";
  }
  return value;
}
