import { describe, expect, it } from 'vitest';
import { formatLocalDateTime, parseUtcApiTimestamp } from '../src/lib/dateTime';

describe('dateTime helpers', () => {
  it('treats timezone-less API timestamps as UTC', () => {
    expect(parseUtcApiTimestamp('2026-05-14T02:52:00').toISOString()).toBe(
      '2026-05-14T02:52:00.000Z'
    );
  });

  it('keeps timestamps that already include a timezone offset', () => {
    expect(parseUtcApiTimestamp('2026-05-14T02:52:00+05:30').toISOString()).toBe(
      '2026-05-13T21:22:00.000Z'
    );
  });

  it('formats UTC timestamps in the requested local timezone', () => {
    expect(
      formatLocalDateTime(
        '2026-05-14T02:52:00',
        {
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
          timeZone: 'Asia/Kolkata',
        },
        'en-US'
      )
    ).toBe('08:22');
  });
});
