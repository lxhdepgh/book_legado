package services

import (
	"fmt"
	"strings"
)

// 拦截页识别（port 自 app/services/block_pages.py）。
//
// 两个层面都会把"不是内容的响应"塞回来：本机安全软件把 body 换成自己的提示页，
// 远端站点躲在 WAF/反爬页后面。解析这两种页面都得到空书单，和"规则坏了"不可区分，
// 所以被拦的源以前看起来像"仅搜索可用"而每个字段都是空的。

var localBlockMarkers = []string{
	"火绒", "安全响应中心", "腾讯电脑管家", "360安全卫士", "360手机卫士",
	"smartscreen", "访问被拦截", "该网站已被拦截", "已阻止你访问该网站",
	"危险网站", "恶意网站", "钓鱼网站", "拦截提示",
}

var remoteBlockMarkers = [][2]string{
	{"attention required! | cloudflare", "Cloudflare"},
	{"you have been blocked", "Cloudflare"},
	{"cf-error-details", "Cloudflare"},
	{"error 1020", "Cloudflare"},
	{"checking your browser before accessing", "Cloudflare 质询页"},
	{"just a moment...", "Cloudflare 质询页"},
	{"enable javascript and cookies to continue", "Cloudflare 质询页"},
	{"incapsula incident id", "Imperva"},
	{"sucuri website firewall", "Sucuri"},
	{"errors.aliyun.com", "阿里云盾"},
	{"yunjiasu", "百度云加速"},
	{"safedog", "安全狗"},
	{"网站防火墙", "站点防火墙"},
}

// 真内容不会这么小又是个拦截页；上限防止引用标记词的长页面被误读。
const maxBlockBody = 20000

// blockReason 当 body 是拦截页时返回一句中文说明，否则回空串。
func blockReason(text string, statusCode int, hasStatus bool) string {
	body := text
	if body == "" || len(body) > maxBlockBody {
		return ""
	}
	lowered := strings.ToLower(body)
	for _, marker := range localBlockMarkers {
		if strings.Contains(lowered, marker) {
			return fmt.Sprintf("本机安全软件已拦截该站点（%s）", marker)
		}
	}
	code := 0
	if hasStatus {
		code = statusCode
	}
	if code != 0 && code < 400 {
		return ""
	}
	for _, pair := range remoteBlockMarkers {
		if strings.Contains(lowered, pair[0]) {
			suffix := ""
			if code != 0 {
				suffix = fmt.Sprintf("，HTTP %d", code)
			}
			return fmt.Sprintf("目标站点拦截了本次访问（%s%s）", pair[1], suffix)
		}
	}
	return ""
}
