pipeline {
    agent any

    environment {
        DB_HOST              = '172.17.0.1'
        DB_PORT              = '3307'
        DB_NAME              = 'stocker'
        DB_USER              = 'jinseoki'
        CLICKHOUSE_HOST      = '172.17.0.1'
        CLICKHOUSE_PORT      = '9000'
        CLICKHOUSE_USER      = 'jinseoki'
        SEC_USER_AGENT       = 'Personal Project (jinseoki10@gmail.com)'
        TZ                   = 'Asia/Seoul'

        DB_PASSWORD          = credentials('DB_PASSWORD')
        CLICKHOUSE_PASSWORD  = credentials('clickhouse-password')
        GEMINI_API_KEY       = credentials('GEMINI_API_KEY')
        GEMINI_MODEL_NAME    = 'gemini-3.1-flash-lite-preview'
    }

    options {
        disableConcurrentBuilds(abortPrevious: true)
    }

    triggers {
        pollSCM('H/5 * * * *')
    }

    stages {
        stage('Checkout') {
            steps {
                deleteDir()
                git branch: 'master',
                    url: 'https://github.com/LeeJinSeok323/stock-collector-bot.git'
            }
        }

        stage('Build Image') {
            steps {
                sh 'docker build --no-cache -t stock-collector-bot:latest .'
            }
        }

        stage('Deploy') {
            steps {
                sh '''
                    docker rm -f stocker-bot || true

                    docker run -d \
                        --name stocker-bot \
                        --restart always \
                        -v /etc/localtime:/etc/localtime:ro \
                        -v /etc/timezone:/etc/timezone:ro \
                        -e TZ=${TZ} \
                        -e DB_HOST=${DB_HOST} \
                        -e DB_PORT=${DB_PORT} \
                        -e DB_NAME=${DB_NAME} \
                        -e DB_USER=${DB_USER} \
                        -e DB_PASSWORD=${DB_PASSWORD} \
                        -e CLICKHOUSE_HOST=${CLICKHOUSE_HOST} \
                        -e CLICKHOUSE_PORT=${CLICKHOUSE_PORT} \
                        -e CLICKHOUSE_USER=${CLICKHOUSE_USER} \
                        -e CLICKHOUSE_PASSWORD=${CLICKHOUSE_PASSWORD} \
                        -e GEMINI_API_KEY=${GEMINI_API_KEY} \
                        -e GEMINI_MODEL_NAME=${GEMINI_MODEL_NAME} \
                        -e SEC_USER_AGENT=${SEC_USER_AGENT} \
                        stock-collector-bot:latest
                '''
            }
        }
    }

    post {
        success { echo 'stock-collector-bot 배포 완료' }
        failure { echo 'stock-collector-bot 배포 실패' }
    }
}
