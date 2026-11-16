package engine

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestTailBytes(t *testing.T) {
	if got := tailBytes("short", 10); got != "short" {
		t.Fatalf("short input changed: %q", got)
	}
	got := tailBytes(strings.Repeat("é", 100), 11) // 2-byte runes: the cut must not split one
	if !utf8.ValidString(got) || !strings.HasPrefix(got, "[... truncated") || !strings.HasSuffix(got, "é") {
		t.Fatalf("bad truncation: %q", got)
	}
}
