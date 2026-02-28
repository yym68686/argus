package nodehost

import (
	"path/filepath"
	"strings"
)

func JoinPath(elem ...string) string {
	return filepath.Join(elem...)
}

func SafeBasename(raw string) string {
	s := strings.TrimSpace(raw)
	if s == "" {
		s = "node"
	}
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z':
			b.WriteRune(r)
		case r >= 'A' && r <= 'Z':
			b.WriteRune(r)
		case r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == '_' || r == '.' || r == '-':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
		if b.Len() >= 120 {
			break
		}
	}
	out := b.String()
	if out == "" {
		return "node"
	}
	return out
}

