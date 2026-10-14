package gateway

import (
	"encoding/json"
	"math"
	"reflect"
)

// ToJSON recursively downgrades a value to JSON-native types.
// Aligns with Python gateway/serialize.py to_json().
func ToJSON(v interface{}) interface{} {
	if v == nil {
		return nil
	}

	switch val := v.(type) {
	case string, int, int64, float64, bool:
		// Handle NaN in float64 - convert to nil
		if f, ok := v.(float64); ok && math.IsNaN(f) {
			return nil
		}
		return val

	case []byte:
		return string(val)

	case json.Number:
		return val.String()

	case map[string]interface{}:
		result := make(map[string]interface{}, len(val))
		for k, item := range val {
			result[k] = ToJSON(item)
		}
		return result

	case []interface{}:
		result := make([]interface{}, len(val))
		for i, item := range val {
			result[i] = ToJSON(item)
		}
		return result

	default:
		// Try to use json.Marshal for complex types
		data, err := json.Marshal(val)
		if err != nil {
			return nil // fallback for unmarshalable types
		}
		var decoded interface{}
		json.Unmarshal(data, &decoded)
		return decoded
	}
}

// Project applies a field whitelist to a list of maps.
// Aligns with Python gateway/serialize.py project().
func Project(items []map[string]interface{}, fields []string) []map[string]interface{} {
	if len(fields) == 0 {
		return items
	}

	fieldSet := make(map[string]bool, len(fields))
	for _, f := range fields {
		fieldSet[f] = true
	}

	result := make([]map[string]interface{}, 0, len(items))
	for _, item := range items {
		projected := make(map[string]interface{})
		for _, key := range fields {
			if val, ok := item[key]; ok {
				projected[key] = val
			}
		}
		result = append(result, projected)
	}
	return result
}

// Encode serializes a payload to a JSON string.
func Encode(payload interface{}) string {
	data, err := json.Marshal(ToJSON(payload))
	if err != nil {
		// Fallback: try encoding as-is
		data, _ = json.Marshal(payload)
	}
	return string(data)
}

// FindLeak checks if a value contains non-JSON leaves.
// Returns empty string if clean, or description of the leak.
func FindLeak(value interface{}) string {
	v := reflect.ValueOf(value)
	return checkJSON(v)
}

func checkJSON(v reflect.Value) string {
	if !v.IsValid() {
		return ""
	}

	switch v.Kind() {
	case reflect.Ptr, reflect.Interface:
		if v.IsNil() {
			return ""
		}
		return checkJSON(v.Elem())

	case reflect.Struct:
		t := v.Type()
		for i := 0; i < v.NumField(); i++ {
			ft := t.Field(i)
			if ft.IsExported() {
				fv := v.Field(i)
				if tag := ft.Tag.Get("json"); tag != "" && tag != "-" {
					if err := checkJSON(fv); err != "" {
						return ft.Name + ": " + err
					}
				}
			}
		}
		return ""

	case reflect.Slice, reflect.Array:
		for i := 0; i < v.Len(); i++ {
			if err := checkJSON(v.Index(i)); err != "" {
				return "[" + err + "]"
			}
		}
		return ""

	case reflect.Map:
		iter := v.MapRange()
		for iter.Next() {
			if err := checkJSON(iter.Value()); err != "" {
				return "map{...}"
			}
		}
		return ""

	case reflect.String:
		return ""

	case reflect.Int, reflect.Int64, reflect.Float64, reflect.Bool:
		return ""

	default:
		return "unsupported type: " + v.Kind().String()
	}
}
