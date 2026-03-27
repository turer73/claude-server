/*
 * proc_linux_ai.c — /proc/linux_ai kernel module
 * Exposes server metrics and configurable thresholds.
 */
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>
#include <linux/sched/loadavg.h>
#include <linux/mm.h>
#include <linux/jiffies.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Linux-AI Server");
MODULE_DESCRIPTION("Proc filesystem exporter for Linux-AI metrics");
MODULE_VERSION("1.0");

static int alert_cpu_threshold = 85;
static int alert_mem_threshold = 85;
static int alert_disk_threshold = 90;
static unsigned long remediation_count = 0;
static unsigned long alert_count = 0;

static int linux_ai_show(struct seq_file *m, void *v)
{
    struct sysinfo si;
    unsigned long total_ram, free_ram, used_ram, uptime_seconds;

    si_meminfo(&si);
    total_ram = si.totalram * si.mem_unit / 1024;
    free_ram = si.freeram * si.mem_unit / 1024;
    used_ram = total_ram - free_ram;
    uptime_seconds = jiffies_to_msecs(jiffies - INITIAL_JIFFIES) / 1000;

    seq_printf(m, "linux_ai_version 1\n");
    seq_printf(m, "linux_ai_uptime_seconds %lu\n", uptime_seconds);
    seq_printf(m, "linux_ai_memory_total_kb %lu\n", total_ram);
    seq_printf(m, "linux_ai_memory_used_kb %lu\n", used_ram);
    seq_printf(m, "linux_ai_memory_free_kb %lu\n", free_ram);
    seq_printf(m, "linux_ai_memory_buffers_kb %lu\n", si.bufferram * si.mem_unit / 1024);
    seq_printf(m, "linux_ai_load_1m %lu.%02lu\n", LOAD_INT(avenrun[0]), LOAD_FRAC(avenrun[0]));
    seq_printf(m, "linux_ai_load_5m %lu.%02lu\n", LOAD_INT(avenrun[1]), LOAD_FRAC(avenrun[1]));
    seq_printf(m, "linux_ai_load_15m %lu.%02lu\n", LOAD_INT(avenrun[2]), LOAD_FRAC(avenrun[2]));
    seq_printf(m, "linux_ai_cpu_count %d\n", num_online_cpus());
    seq_printf(m, "linux_ai_procs_running %u\n", si.procs);
    seq_printf(m, "linux_ai_agent_alerts_total %lu\n", alert_count);
    seq_printf(m, "linux_ai_agent_remediations_total %lu\n", remediation_count);
    seq_printf(m, "linux_ai_threshold_cpu %d\n", alert_cpu_threshold);
    seq_printf(m, "linux_ai_threshold_memory %d\n", alert_mem_threshold);
    seq_printf(m, "linux_ai_threshold_disk %d\n", alert_disk_threshold);
    return 0;
}

static int linux_ai_open(struct inode *inode, struct file *file)
{ return single_open(file, linux_ai_show, NULL); }

static const struct proc_ops linux_ai_ops = {
    .proc_open = linux_ai_open, .proc_read = seq_read,
    .proc_lseek = seq_lseek, .proc_release = single_release,
};

static ssize_t config_write(struct file *file, const char __user *buf, size_t count, loff_t *ppos)
{
    char kbuf[64], key[32];
    int val;
    if (count >= sizeof(kbuf)) return -EINVAL;
    if (copy_from_user(kbuf, buf, count)) return -EFAULT;
    kbuf[count] = '\0';
    if (sscanf(kbuf, "%31s %d", key, &val) != 2) return -EINVAL;
    if (strcmp(key, "alert_cpu") == 0 && val > 0 && val <= 100) alert_cpu_threshold = val;
    else if (strcmp(key, "alert_mem") == 0 && val > 0 && val <= 100) alert_mem_threshold = val;
    else if (strcmp(key, "alert_disk") == 0 && val > 0 && val <= 100) alert_disk_threshold = val;
    else if (strcmp(key, "inc_alerts") == 0) alert_count += val;
    else if (strcmp(key, "inc_remediations") == 0) remediation_count += val;
    else return -EINVAL;
    return count;
}

static int config_show(struct seq_file *m, void *v)
{
    seq_printf(m, "alert_cpu %d\nalert_mem %d\nalert_disk %d\nalerts_total %lu\nremediations_total %lu\n",
               alert_cpu_threshold, alert_mem_threshold, alert_disk_threshold, alert_count, remediation_count);
    return 0;
}

static int config_open(struct inode *inode, struct file *file)
{ return single_open(file, config_show, NULL); }

static const struct proc_ops config_ops = {
    .proc_open = config_open, .proc_read = seq_read, .proc_write = config_write,
    .proc_lseek = seq_lseek, .proc_release = single_release,
};

static struct proc_dir_entry *proc_entry, *proc_config;

static int __init linux_ai_init(void)
{
    proc_entry = proc_create("linux_ai", 0444, NULL, &linux_ai_ops);
    if (!proc_entry) return -ENOMEM;
    proc_config = proc_create("linux_ai_config", 0644, NULL, &config_ops);
    if (!proc_config) { proc_remove(proc_entry); return -ENOMEM; }
    pr_info("linux_ai: proc entries created\n");
    return 0;
}

static void __exit linux_ai_exit(void)
{
    proc_remove(proc_config);
    proc_remove(proc_entry);
    pr_info("linux_ai: proc entries removed\n");
}

module_init(linux_ai_init);
module_exit(linux_ai_exit);
