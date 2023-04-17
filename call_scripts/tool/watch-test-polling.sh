function default_setting() {
    twcc=False
    sleep_time=3600
    arch=nat_pretrained_model
    
}


VALID_ARGS=$(getopt -o e: --long experiment:,twcc,sleep:,arch: -- "$@")
if [[ $? -ne 0 ]]; then
    exit 1;
fi

eval set -- "$VALID_ARGS"
default_setting

while [ : ]; do
  case "$1" in 
    -e | --experiment)
        experiment_ids="$2"     
        echo "$experiment_ids" 
        exp_array+=("$experiment_ids")
        shift 2
        ;;
    --arch)
      arch="$2"
      shift 2
      ;;          
    --twcc)
      twcc=True
      shift 1
      ;;        
    --sleep)
      sleep_time=$2
      shift 2
      ;;                
    --) shift; 
        break
  esac
done


while :
do

   for i in "${!exp_array[@]}"; do 
      experiment_id=${exp_array[$i]}
      CHECKPOINT=checkpoints/$experiment_id
      if [ ! -d "$CHECKPOINT" ]; then
         echo "Folder is not exist"
         continue
      fi       
      dt=$(date)
      if [ "$twcc" = "True" ]
      then
         echo "Wait TWCC Resource"
         bash call_scripts/generate_nat.sh -e $experiment_id -b 50 --ck-types last-top --has-eos --arch $arch > tmp_file
      else
         echo "Wait Battleship Resource"
         random_num=$RANDOM
         bash call_scripts/generate_nat.sh -e $experiment_id -b 50 --ck-types last-top --arch $arch > tmp_file_$random_num
      fi
      score=$(tail -1 tmp_file_$random_num) 
      echo $dt ': ' $'\t' $score >> $CHECKPOINT/best_top5.test.record
      rm tmp_file_$random_num
   done
   date
   echo "Sleeping Now : $sleep_time s"
   sleep $sleep_time

done

