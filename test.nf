import static Globals.*
class Globals {
    static final String SEPARATOR = '-+-'
}

def absPath(path) {
    file(path)*.toAbsolutePath()
}

def str(val) {
    if (val instanceof java.nio.file.Path) {
        val.name - ~/[\.][^\.]+$/
    }
    else {
        val
    }
}

def joiner(row, sweepBegin=0, sweepEnd=-1) {
    [row[sweepBegin..sweepEnd].collect { str(it) }.join(SEPARATOR), *row]
}

def select(row, elems) {
    row[elems]
}

trees = absPath('test/trees/*nwk')
profiles = absPath('test/tables/*table')
ancestor = [file('test/ancestor.fa')]
donor = [file('test/donor.fa')]

alpha_BL = [1,0.5]
xfold = [10,20]
n3c = [50000,100000]


next = Channel
    .from(ancestor)
    .spread(donor)
    .spread(alpha_BL)
    .spread(trees)
    .map{ joiner(it) }
    .into(2)

process Evolve {

    input:
    set key, ancestor, donor, alpha, tree from next

    output:
    set file("${key}.evo.fa"), ancestor, donor, alpha, tree into evo_out

    """
    scale_tree.py -a $alpha $tree scaled_tree
    \$EXT_BIN/sgevolver/sgEvolver --indel-freq=${params.indel_freq} --small-ht-freq=${params.small_ht_freq} \
        --large-ht-freq=${params.large_ht_freq} --inversion-freq=${params.inversion_freq} \
        --random-seed=${params.seed} scaled_tree \
         $ancestral $donor "${key}.evo.aln" "${key}.evo.fa"
    strip_semis.sh "${key}.evo.fa"
    """
}


(evo_out, next) = evo_out.into(2)

next = next.spread(profiles)
    .spread(xfold)
    .map{ joiner(it, 1) }
    
process WGS_Reads {

    input:
    set key, ref_seq, ancestor, donor, alpha, tree, profile, xfold from next

    output:
    set file("${key}.wgs.r*.fq.gz"), ref_seq, ancestor, donor, alpha, tree, profile, xfold into wgs_out

    """
    export PATH=\$EXT_BIN/art:\$PATH
    metaART.py -C gzip -t $profile -z 1 -M $xfold -S ${params.seed} -s ${params.wgs_ins_std} \
            -m ${params.wgs_ins_len} -l ${params.wgs_read_len} -n "${key}.wgs" $ref_seq .
    """
}


(evo_out, next) = evo_out.into(2)
next = next.spread(profiles)
        .spread(n3c)
        .map { joiner(it, 1) }

process HIC_Reads {

    input:
    set key, ref_seq, ancestor, donor, alpha, tree, profile, n3c from next

    output:
    set file("${key}.hic.fa.gz"), ref_seq, ancestor, donor, alpha, tree, profile, n3c into hic_out

    """
    simForward.py -C gzip -r ${params.seed} -n $n3c -l ${params.hic_read_len} -p ${params.hic_inter_prob} \
           -t $profile $descendent "${key}.hic.fa.gz"
    """
}


(wgs_out, next) = wgs_out.into(2)
next = next
    .map { row -> row.flatten() }
    .map{ joiner(it, 3) }

process Assemble {

    input:
    set key, R1, R2, ref_seq, ancestor, donor, alpha, tree, profile, xfold from next

    output:
    set file("${key}.contigs.fasta"), ref_seq, ancestor, donor, alpha, tree, profile, xfold into asm_out

    """
    \$EXT_BIN/a5/bin/a5_pipeline.pl --threads=1 --metagenome $R1 $R2 ${key}
    """
}


(asm_out, next) = asm_out.into(2)
next = next.map { joiner(it, 2) }

process Truth {

    input:
    set key, contigs, ref_seq, ancestor, donor, alpha, tree, profile, xfold from next

    output:
    set file("${key}.truth"), contigs, ref_seq, ancestor, donor, alpha, tree, profile, xfold into truth_out

    """
    if [ ! -e db.prj ]
    then
        lastdb db $ref_seq
    fi

    \$EXT_BIN/last/lastal -P 1 db $contigs | maf-convert psl > ctg2ref.psl
    alignmentToTruth.py --ofmt json ctg2ref.psl "${key}.truth"
    """
}


(asm_out, next) = asm_out.into(2)
next = hic_out
    .map { joiner(it, 2, 6) }
    .cross(next
        .map { joiner(it, 2, 6) })
    .map { it.flatten() }
    .map { it.unique() }
    .map { select(it, [1,9,3..7,10,8]) }
    .map { joiner(it, 2) }

process HiCMap {

    input:
    set key, hic_reads, contigs, ancestor, donor, alpha, tree, profile, xfold, n3c from next

    output:
    set file("${key}.hic2ctg*"), ancestor, donor, alpha, tree, profile, xfold, n3c into hicmap_out

    """
    export PATH=\$EXT_BIN/a5/bin:\$PATH
    bwa index $contigs
    bwa mem -t 1 $contigs $hic_reads | samtools view -bS - | samtools sort -l 9 - "${key}.hic2ctg"
    samtools index "${key}.hic2ctg.bam"
    samtools idxstats "${key}.hic2ctg.bam" > "${key}.hic2ctg.idxstats"
    samtools flagstat "${key}.hic2ctg.bam" > "${key}.hic2ctg.flagstat"
    """
}


next = hicmap_out
    .map { it.flatten() }
    .map { joiner(it, 2) }
    
process Graph {

    input:
    set key, hic_bam, hic_bai, ancestor, donor, alpha, tree, profile, xfold, n3c from next

    output:
    set file("${key}.graphml"), ancestor, donor, alpha, tree, profile, xfold, n3c into graph_out

    """
    bamToEdges_mod2.py --sim --afmt bam --strong 150 --graphml "${key}.graphml" --merged $hic_bam hic2ctg.e hic2ctg.n
    """
}


(wgs_out, next) = wgs_out.into(2)
(asm_out, tmp) = asm_out.into(2)
next = next
    .map { it.flatten() }
    .map { joiner(it, 3) }
    .cross(tmp
        .map { joiner(it, 2) })
    .map { it.flatten() }
    .map { it.unique() }
    .map { select(it, [1,2,10,4..9]) }
    .map { joiner(it, 3) }

process WGSMap {

    input:
    set key, R1, R2, contigs, ancestor, donor, alpha, tree, profile, xfold from next

    output:
    set file("${key}.wgs2ctg.bam"), ancestor, donor, alpha, tree, profile, xfold into wgsmap_out

    """
    export PATH=\$EXT_BIN/a5/bin:\$PATH
    bwa index $contigs
    bwa mem -t 1 $contigs $R1 $R2 | samtools view -bS - | samtools sort -l 9 - "${key}.wgs2ctg"
    """
}


(wgsmap_out, next) = wgsmap_out.into(2)
next = next.map { joiner(it, 1) }

process InferReadDepth {

    input:
    set key, wgs_bam, ancestor, donor, alpha, tree, profile, xfold from next

    output:
    set file("${key}.wgs2ctg.cov"), ancestor, donor, alpha, tree, profile, xfold into cov_out

    """
    \$EXT_BIN/bedtools/bedtools genomecov -ibam $wgs_bam | \
    awk '
    BEGIN{n=0}
    {
        # ignore whole genome records
        if (\$1 != "genome") {
            # store names as they appear in repository
            # we use this to preserve file order
            if (!(\$1 in seq_cov)) {
                name_repo[n++]=\$1
            # sum uses relative weights from histogram
            }
            seq_cov[\$1]+=\$2*\$3/\$4
        }
    }
    END{
        for (i=0; i<n; i++) {
            print i+1, name_repo[i], seq_cov[name_repo[i]]
        }
    }' > "${key}.wgs2ctg.cov"
    """
}