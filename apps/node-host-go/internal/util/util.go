package util

import (
	"bytes"
	"crypto/rand"
	"crypto/sha1"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"time"
)

func NowUnixMs() int64 { return time.Now().UnixNano() / int64(time.Millisecond) }

func ParseBool(raw string, defaultValue bool) bool {
	s := strings.TrimSpace(strings.ToLower(raw))
	if s == "" {
		return defaultValue
	}
	switch s {
	case "1", "true", "yes", "y", "on":
		return true
	case "0", "false", "no", "n", "off":
		return false
	default:
		return defaultValue
	}
}

func ClampInt(v int, min int, max int) int {
	if v < min {
		return min
	}
	if v > max {
		return max
	}
	return v
}

func ClampFloat64(v float64, min float64, max float64) float64 {
	if v < min {
		return min
	}
	if v > max {
		return max
	}
	return v
}

func TruncateUTF8Bytes(s string, maxBytes int) string {
	if maxBytes <= 0 {
		return ""
	}
	b := []byte(s)
	if len(b) <= maxBytes {
		return s
	}
	return string(b[:maxBytes]) + "…"
}

func SafeJSON(v interface{}) string {
	b, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	return string(b)
}

func RedactWSURL(raw string) string {
	s := strings.TrimSpace(raw)
	u, err := url.Parse(s)
	if err != nil || u == nil || u.Scheme == "" || u.Host == "" {
		reToken := regexp.MustCompile(`(?i)([?&]token=)[^&]*`)
		return reToken.ReplaceAllString(s, `$1***`)
	}

	if u.User != nil {
		u.User = url.UserPassword("***", "***")
	}
	sensitive := map[string]struct{}{
		"token":        {},
		"access_token": {},
		"auth":         {},
		"apikey":       {},
		"api_key":      {},
		"key":          {},
		"secret":       {},
		"signature":    {},
		"sig":          {},
	}
	q := u.Query()
	for k := range q {
		if _, ok := sensitive[strings.ToLower(k)]; ok {
			q.Set(k, "***")
		}
	}
	u.RawQuery = q.Encode()
	return u.String()
}

func ComputeReconnectDelayMs(baseMs int, maxMs int, failures int, jitterPct float64, randFloat func() float64) int {
	safeBase := baseMs
	if safeBase < 0 {
		safeBase = 0
	}
	safeMax := maxMs
	if safeMax < safeBase {
		safeMax = safeBase
	}
	safeFailures := failures
	if safeFailures < 0 {
		safeFailures = 0
	}
	safeJitter := ClampFloat64(jitterPct, 0, 0.5)

	exp := safeFailures - 1
	if exp < 0 {
		exp = 0
	}
	if exp > 12 {
		exp = 12
	}
	raw := float64(safeBase) * float64(uint64(1)<<uint(exp))
	capped := raw
	if capped > float64(safeMax) {
		capped = float64(safeMax)
	}
	if capped <= 0 {
		return 0
	}
	if safeJitter <= 0 {
		return int(capped)
	}
	jitter := capped * safeJitter
	min := capped - jitter
	if min < 0 {
		min = 0
	}
	max := capped + jitter
	if randFloat == nil {
		randFloat = func() float64 { return 0.5 }
	}
	return int(min + randFloat()*(max-min))
}

func RandBase64(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(b), nil
}

func WebSocketAcceptKey(secKey string) string {
	const magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
	h := sha1.Sum([]byte(secKey + magic))
	return base64.StdEncoding.EncodeToString(h[:])
}

func RandomUUID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	hex32 := hex.EncodeToString(b[:])
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		hex32[0:8],
		hex32[8:12],
		hex32[12:16],
		hex32[16:20],
		hex32[20:32],
	), nil
}

func RandomHex(nBytes int) (string, error) {
	b := make([]byte, nBytes)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func SortStringsCopy(in []string) []string {
	out := make([]string, 0, len(in))
	out = append(out, in...)
	sort.Strings(out)
	return out
}

func LastNRunes(s string, n int) string {
	if n <= 0 {
		return ""
	}
	if len(s) <= n {
		return s
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[len(r)-n:])
}

func PrettyJSONLine(v interface{}) string {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return ""
	}
	return string(bytes.TrimRight(b, "\n")) + "\n"
}
