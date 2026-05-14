const EXPLICIT_TIME_ZONE_SUFFIX = /(z|[+-]\d{2}:?\d{2})$/i;

export function parseUtcApiTimestamp(timestamp: string): Date {
  const normalized = timestamp.trim();
  const timestampWithTimeZone = EXPLICIT_TIME_ZONE_SUFFIX.test(normalized)
    ? normalized
    : `${normalized}Z`;

  return new Date(timestampWithTimeZone);
}

export function formatLocalDateTime(
  timestamp: string,
  options: Intl.DateTimeFormatOptions,
  locales?: Intl.LocalesArgument
): string {
  return parseUtcApiTimestamp(timestamp).toLocaleString(locales, options);
}
