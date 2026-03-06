package jobstore

import "strings"

const (
	ttyESC = "\x1b"
	ttyCR  = "\r"
	ttyTAB = "\t"
)

var ttyNamedKeyMap = map[string]string{
	"enter":     ttyCR,
	"return":    ttyCR,
	"tab":       ttyTAB,
	"up":        ttyESC + "[A",
	"down":      ttyESC + "[B",
	"right":     ttyESC + "[C",
	"left":      ttyESC + "[D",
	"esc":       ttyESC,
	"escape":    ttyESC,
	"space":     " ",
	"backspace": "\x7f",
	"bspace":    "\x7f",
}

func EncodeKeySequence(keys []string, literal string) (string, []string) {
	var builder strings.Builder
	warnings := make([]string, 0)
	if literal != "" {
		builder.WriteString(literal)
	}
	for _, key := range keys {
		encoded, ok := encodeKeyToken(key)
		if !ok {
			warnings = append(warnings, "Unknown key: "+key)
			continue
		}
		builder.WriteString(encoded)
	}
	return builder.String(), warnings
}

func EncodePaste(text string, bracketed bool) string {
	if !bracketed {
		return text
	}
	return ttyESC + "[200~" + text + ttyESC + "[201~"
}

func encodeKeyToken(raw string) (string, bool) {
	key := strings.TrimSpace(raw)
	if key == "" {
		return "", false
	}
	lower := strings.ToLower(key)
	if encoded, ok := ttyNamedKeyMap[lower]; ok {
		return encoded, true
	}
	if strings.HasPrefix(lower, "c-") || strings.HasPrefix(lower, "ctrl-") {
		rest := key[strings.Index(key, "-")+1:]
		if len(rest) == 1 {
			ch := rest[0]
			if ch >= 'a' && ch <= 'z' {
				return string(ch - 'a' + 1), true
			}
			if ch >= 'A' && ch <= 'Z' {
				return string(ch - 'A' + 1), true
			}
		}
	}
	if len(key) == 1 {
		return key, true
	}
	return "", false
}
