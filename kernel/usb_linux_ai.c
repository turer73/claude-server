/*
 * usb_linux_ai.c — USB whitelist module
 * Logs USB connections, optionally blocks unknown devices.
 * Managed via /proc/linux_ai_usb
 */
#include <linux/module.h>
#include <linux/usb.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Linux-AI Server");
MODULE_DESCRIPTION("USB device whitelist with logging");
MODULE_VERSION("1.0");

#define MAX_WL 32
#define MAX_LOG 64

struct usb_id { u16 vendor; u16 product; };
struct usb_event { u16 vendor; u16 product; char action[16]; unsigned long ts; };

static struct usb_id whitelist[MAX_WL];
static int wl_count = 0;
static bool enforce = false;
static DEFINE_SPINLOCK(usb_lock);

static struct usb_event ulog[MAX_LOG];
static int log_idx = 0;
static atomic_t total_conn = ATOMIC_INIT(0);
static atomic_t blocked_conn = ATOMIC_INIT(0);

static void log_event(u16 v, u16 p, const char *act)
{
    struct usb_event *e = &ulog[log_idx % MAX_LOG];
    e->vendor = v; e->product = p; e->ts = jiffies;
    strscpy(e->action, act, sizeof(e->action));
    log_idx++;
}

static bool is_allowed(u16 v, u16 p)
{
    int i;
    for (i = 0; i < wl_count; i++)
        if (whitelist[i].vendor == v && whitelist[i].product == p) return true;
    return false;
}

static int usb_notify(struct notifier_block *nb, unsigned long action, void *data)
{
    struct usb_device *dev = data;
    u16 vid, pid;
    if (!dev) return NOTIFY_OK;
    vid = le16_to_cpu(dev->descriptor.idVendor);
    pid = le16_to_cpu(dev->descriptor.idProduct);

    if (action == USB_DEVICE_ADD) {
        atomic_inc(&total_conn);
        spin_lock(&usb_lock);
        if (enforce && !is_allowed(vid, pid)) {
            log_event(vid, pid, "blocked");
            atomic_inc(&blocked_conn);
            spin_unlock(&usb_lock);
            pr_warn("linux_ai_usb: BLOCKED %04x:%04x\n", vid, pid);
            /* Deauthorize via sysfs: echo 0 > /sys/bus/usb/devices/.../authorized */
            dev->authorized = 0;
            return NOTIFY_OK;
        }
        log_event(vid, pid, "connected");
        spin_unlock(&usb_lock);
    } else if (action == USB_DEVICE_REMOVE) {
        spin_lock(&usb_lock);
        log_event(vid, pid, "disconnected");
        spin_unlock(&usb_lock);
    }
    return NOTIFY_OK;
}

static struct notifier_block usb_nb = { .notifier_call = usb_notify };

static int usb_show(struct seq_file *m, void *v)
{
    int i, count, start;
    seq_printf(m, "enforce %d\ntotal_connections %d\nblocked_connections %d\nwhitelist_count %d\n",
               enforce ? 1 : 0, atomic_read(&total_conn), atomic_read(&blocked_conn), wl_count);
    spin_lock(&usb_lock);
    for (i = 0; i < wl_count; i++)
        seq_printf(m, "allowed %04x:%04x\n", whitelist[i].vendor, whitelist[i].product);
    count = (log_idx < MAX_LOG) ? log_idx : MAX_LOG;
    start = (log_idx < MAX_LOG) ? 0 : log_idx % MAX_LOG;
    for (i = 0; i < count; i++) {
        struct usb_event *e = &ulog[(start + i) % MAX_LOG];
        seq_printf(m, "event %04x:%04x %s %lu\n", e->vendor, e->product, e->action, e->ts);
    }
    spin_unlock(&usb_lock);
    return 0;
}

static int usb_open(struct inode *inode, struct file *file)
{ return single_open(file, usb_show, NULL); }

static ssize_t usb_write(struct file *file, const char __user *buf, size_t count, loff_t *ppos)
{
    char kbuf[64], cmd[16];
    unsigned int vid, pid;
    int i, val;

    if (count >= sizeof(kbuf)) return -EINVAL;
    if (copy_from_user(kbuf, buf, count)) return -EFAULT;
    kbuf[count] = '\0';

    if (sscanf(kbuf, "enforce %d", &val) == 1) { enforce = (val != 0); return count; }
    if (sscanf(kbuf, "%15s %4x:%4x", cmd, &vid, &pid) != 3) return -EINVAL;

    spin_lock(&usb_lock);
    if (strcmp(cmd, "allow") == 0 && wl_count < MAX_WL && !is_allowed(vid, pid)) {
        whitelist[wl_count].vendor = vid;
        whitelist[wl_count].product = pid;
        wl_count++;
    } else if (strcmp(cmd, "deny") == 0) {
        for (i = 0; i < wl_count; i++)
            if (whitelist[i].vendor == vid && whitelist[i].product == pid)
                { whitelist[i] = whitelist[--wl_count]; break; }
    }
    spin_unlock(&usb_lock);
    return count;
}

static const struct proc_ops usb_ops = {
    .proc_open = usb_open, .proc_read = seq_read, .proc_write = usb_write,
    .proc_lseek = seq_lseek, .proc_release = single_release,
};

static struct proc_dir_entry *usb_proc;

static int __init usb_init(void)
{
    usb_proc = proc_create("linux_ai_usb", 0644, NULL, &usb_ops);
    if (!usb_proc) return -ENOMEM;
    usb_register_notify(&usb_nb);
    pr_info("linux_ai_usb: loaded\n");
    return 0;
}

static void __exit usb_exit(void)
{
    usb_unregister_notify(&usb_nb);
    proc_remove(usb_proc);
    pr_info("linux_ai_usb: unloaded\n");
}

module_init(usb_init);
module_exit(usb_exit);
