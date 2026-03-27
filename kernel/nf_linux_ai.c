/*
 * nf_linux_ai.c — Netfilter firewall for Linux-AI Server
 * IP blocking + packet counting via /proc/linux_ai_firewall
 */
#include <linux/module.h>
#include <linux/netfilter.h>
#include <linux/netfilter_ipv4.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>
#include <linux/inet.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Linux-AI Server");
MODULE_DESCRIPTION("Netfilter firewall with IP blocking and packet counting");
MODULE_VERSION("1.0");

#define MAX_BLOCKED 64

static __be32 blocked_ips[MAX_BLOCKED];
static int blocked_count = 0;
static DEFINE_SPINLOCK(block_lock);

static atomic64_t total_pkts = ATOMIC64_INIT(0);
static atomic64_t blocked_pkts = ATOMIC64_INIT(0);
static atomic64_t tcp_pkts = ATOMIC64_INIT(0);
static atomic64_t udp_pkts = ATOMIC64_INIT(0);
static atomic64_t api_pkts = ATOMIC64_INIT(0);

static unsigned int nf_hook_fn(void *priv, struct sk_buff *skb, const struct nf_hook_state *state)
{
    struct iphdr *iph;
    struct tcphdr *tcph;
    __be32 src;
    int i;

    if (!skb) return NF_ACCEPT;
    iph = ip_hdr(skb);
    if (!iph) return NF_ACCEPT;

    src = iph->saddr;
    atomic64_inc(&total_pkts);

    spin_lock(&block_lock);
    for (i = 0; i < blocked_count; i++) {
        if (blocked_ips[i] == src) {
            spin_unlock(&block_lock);
            atomic64_inc(&blocked_pkts);
            return NF_DROP;
        }
    }
    spin_unlock(&block_lock);

    if (iph->protocol == IPPROTO_TCP) {
        atomic64_inc(&tcp_pkts);
        tcph = tcp_hdr(skb);
        if (tcph && ntohs(tcph->dest) == 8420)
            atomic64_inc(&api_pkts);
    } else if (iph->protocol == IPPROTO_UDP) {
        atomic64_inc(&udp_pkts);
    }
    return NF_ACCEPT;
}

static struct nf_hook_ops nf_ops = {
    .hook = nf_hook_fn, .pf = PF_INET,
    .hooknum = NF_INET_PRE_ROUTING, .priority = NF_IP_PRI_FIRST,
};

static int fw_show(struct seq_file *m, void *v)
{
    int i;
    seq_printf(m, "total_packets %lld\n", atomic64_read(&total_pkts));
    seq_printf(m, "blocked_packets %lld\n", atomic64_read(&blocked_pkts));
    seq_printf(m, "tcp_packets %lld\n", atomic64_read(&tcp_pkts));
    seq_printf(m, "udp_packets %lld\n", atomic64_read(&udp_pkts));
    seq_printf(m, "api_port_8420 %lld\n", atomic64_read(&api_pkts));
    seq_printf(m, "blocked_count %d\n", blocked_count);
    spin_lock(&block_lock);
    for (i = 0; i < blocked_count; i++)
        seq_printf(m, "blocked %pI4\n", &blocked_ips[i]);
    spin_unlock(&block_lock);
    return 0;
}

static int fw_open(struct inode *inode, struct file *file)
{ return single_open(file, fw_show, NULL); }

static ssize_t fw_write(struct file *file, const char __user *buf, size_t count, loff_t *ppos)
{
    char kbuf[64], cmd[16], ip_str[16];
    __be32 addr;
    int i;

    if (count >= sizeof(kbuf)) return -EINVAL;
    if (copy_from_user(kbuf, buf, count)) return -EFAULT;
    kbuf[count] = '\0';

    if (sscanf(kbuf, "%15s %15s", cmd, ip_str) != 2) return -EINVAL;
    addr = in_aton(ip_str);

    if (strcmp(cmd, "block") == 0) {
        spin_lock(&block_lock);
        if (blocked_count < MAX_BLOCKED) {
            for (i = 0; i < blocked_count; i++)
                if (blocked_ips[i] == addr) { spin_unlock(&block_lock); return count; }
            blocked_ips[blocked_count++] = addr;
        }
        spin_unlock(&block_lock);
    } else if (strcmp(cmd, "unblock") == 0) {
        spin_lock(&block_lock);
        for (i = 0; i < blocked_count; i++) {
            if (blocked_ips[i] == addr) { blocked_ips[i] = blocked_ips[--blocked_count]; break; }
        }
        spin_unlock(&block_lock);
    } else if (strcmp(cmd, "reset") == 0) {
        atomic64_set(&total_pkts, 0); atomic64_set(&blocked_pkts, 0);
        atomic64_set(&tcp_pkts, 0); atomic64_set(&udp_pkts, 0); atomic64_set(&api_pkts, 0);
    }
    return count;
}

static const struct proc_ops fw_ops = {
    .proc_open = fw_open, .proc_read = seq_read, .proc_write = fw_write,
    .proc_lseek = seq_lseek, .proc_release = single_release,
};

static struct proc_dir_entry *fw_proc;

static int __init nf_init(void)
{
    int ret = nf_register_net_hook(&init_net, &nf_ops);
    if (ret) return ret;
    fw_proc = proc_create("linux_ai_firewall", 0644, NULL, &fw_ops);
    if (!fw_proc) { nf_unregister_net_hook(&init_net, &nf_ops); return -ENOMEM; }
    pr_info("linux_ai_fw: loaded\n");
    return 0;
}

static void __exit nf_exit(void)
{
    proc_remove(fw_proc);
    nf_unregister_net_hook(&init_net, &nf_ops);
    pr_info("linux_ai_fw: unloaded\n");
}

module_init(nf_init);
module_exit(nf_exit);
